from django.db import migrations


def separate_worksites(apps, schema_editor):
    alias = schema_editor.connection.alias
    # Use the same clock -> affair -> labor lock order as live settlement.
    Clock = apps.get_model("game_time", "GlobalGameTimeState")
    Clock.objects.using(alias).select_for_update().filter(pk=1).first()
    Affair = apps.get_model("village", "VillageAffair")
    row = Affair.objects.using(alias).select_for_update().filter(pk="BRIDGE_REPAIR").first()
    if row:
        changed = False
        for stage in row.requirements.get("stages", []):
            place = {"legacy_preparation": "REPAIR_YARD", "legacy_work": "OLD_BRIDGE"}.get(stage["id"])
            if place is None:
                continue
            changed |= stage.get("location_code") != place
            stage["location_code"] = place
            for condition in stage["conditions"]:
                changed |= condition.get("locations") != [place] or "location_code" in condition
                condition.pop("location_code", None)
                condition["locations"] = [place]
        if changed:
            row.version += 1
            row.save(using=alias, update_fields=["requirements", "version"])
            Labor = apps.get_model("village", "VillagePlayerAffairLabor")
            Labor.objects.using(alias).filter(affair=row, condition_id="legacy_labor", status="ACTIVE").exclude(
                location_code="OLD_BRIDGE").update(status="CANCELLED", stop_reason="CONDITION_CHANGED", accumulated_minutes=0)

    Location = apps.get_model("village", "VillageLocationDefinition")
    for code, center in (("OLD_BRIDGE", [61.98, -0.6, -0.13]), ("REPAIR_YARD", [45.26, 0, -10.31])):
        location = Location.objects.using(alias).filter(location_code=code).first()
        if location and (location.bounds or {}).get("center") != center:
            location.bounds = {**(location.bounds or {}), "center": center}
            location.save(using=alias, update_fields=["bounds"])


class Migration(migrations.Migration):
    dependencies = [("village", "0061_public_meals_without_authorization")]
    operations = [migrations.RunPython(separate_worksites, migrations.RunPython.noop)]
