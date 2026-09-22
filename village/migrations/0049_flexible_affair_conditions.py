from django.db import migrations, models


MEAL_IDS = [29201, 29202, 29203, 29204, 29205]


def migrate_conditions(apps, schema_editor):
    Affair = apps.get_model("village", "VillageAffair")
    Contribution = apps.get_model("village", "VillageAffairContribution")
    for row in Affair.objects.using(schema_editor.connection.alias).filter(kind="RESOURCE_PROJECT").iterator():
        if "stages" in row.requirements:
            continue
        req, old, places = row.requirements, row.progress, list(row.locations or [])
        preparation, stages = [], []
        progress = {f"legacy_material_{item}": amount for item, amount in old.get("materials", {}).items()}
        for field, condition_id in (("meals", "legacy_meals"), ("gold", "legacy_gold"), ("labor", "legacy_labor")):
            if field in old:
                progress[condition_id] = old[field]
        for item, target in req.get("materials", {}).items():
            cid = f"legacy_material_{item}"
            preparation.append(dict(id=cid, type="item", label="", item_ids=[int(item)], target=target,
                locations=places, min_quality=0, requires_freshness=False, min_remaining_minutes=0))
            progress[cid] = old.get("materials", {}).get(str(item), 0)
        if req.get("meals"):
            preparation.append(dict(id="legacy_meals", type="item", label="成品餐食", item_ids=MEAL_IDS,
                target=req["meals"], locations=places,
                min_quality=1, requires_freshness=True, min_remaining_minutes=0))
            progress["legacy_meals"] = old.get("meals", 0)
        if req.get("gold"):
            preparation.append(dict(id="legacy_gold", type="gold", label="", target=req["gold"], locations=places))
            progress["legacy_gold"] = old.get("gold", 0)
        if preparation:
            stages.append(dict(id="legacy_preparation", title="筹备", location_code="", conditions=preparation))
        if req.get("labor"):
            stages.append(dict(id="legacy_work", title="劳动", location_code="", conditions=[dict(id="legacy_labor", type="labor",
                label="", target=req["labor"], unit_minutes=60, locations=places)]))
            progress["legacy_labor"] = old.get("labor", 0)
        row.requirements = {"stages": stages}
        row.progress = {**old, "conditions": progress}
        if row.status != "COMPLETED":
            row.status = "ACTIVE"
        row.save(update_fields=["requirements", "progress", "status"])
        for contribution in Contribution.objects.using(schema_editor.connection.alias).filter(affair_id=row.pk).iterator():
            contribution.condition_id = (f"legacy_material_{contribution.item_id}" if contribution.kind in {"material", "item"}
                else "legacy_meals" if contribution.kind == "meal" else "legacy_gold" if contribution.kind in {"gold", "fund"}
                else "legacy_labor")
            contribution.save(update_fields=["condition_id"])


class Migration(migrations.Migration):
    dependencies = [("village", "0048_affair_admin_labels")]
    operations = [
        migrations.AddField(model_name="villageaffaircontribution", name="condition_id",
            field=models.CharField(blank=True, default="", max_length=64)),
        migrations.RemoveConstraint(model_name="villageaffaircontribution", name="village_affair_contribution_once"),
        migrations.RunPython(migrate_conditions, migrations.RunPython.noop),
        migrations.AddConstraint(model_name="villageaffaircontribution", constraint=models.UniqueConstraint(
            fields=("affair", "request_id", "condition_id", "kind", "item_id"), name="village_affair_contribution_once")),
    ]
