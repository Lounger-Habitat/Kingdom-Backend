from django.db import migrations


PLOT_CODES = tuple(
    code
    for region in ("A", "B", "C", "D")
    for code in (
        ([f"FARM_PLOT_{region}"] if region != "D" else ["FARM_PLOT_D_01"])
        + [f"FARM_PLOT_{region}_{index:02d}" for index in range(2, 10)]
    )
)

SEED_ITEMS = (
    (29301, "小麦种子", "Bill农田种植小麦所需的种子，可由Nora通过商队进货。", 1005),
    (29302, "胡萝卜种子", "Bill农田种植胡萝卜所需的种子，可由Nora通过商队进货。", 1025),
    (29303, "土豆种薯", "Bill农田种植土豆所需的种薯，可由Nora通过商队进货。", 1042),
)


def add_bill_farm_catalog(apps, schema_editor):
    Item = apps.get_model("items", "Item")
    FarmPlot = apps.get_model("village", "VillageFarmPlot")
    SimulationConfig = apps.get_model("village", "VillageSimulationConfig")
    alias = schema_editor.connection.alias

    for item_id, item_name, description, icon_source_id in SEED_ITEMS:
        icon_source = Item.objects.using(alias).filter(item_id=icon_source_id).first()
        defaults = {
            "item_name": item_name,
            "item_type": "Seed",
            "item_description": description,
            "item_price": 5,
            "collectable": False,
        }
        if icon_source is not None:
            defaults["icon_url"] = icon_source.icon_url
            defaults["icon_image"] = icon_source.icon_image.name or None
        item, created = Item.objects.using(alias).get_or_create(
            item_id=item_id,
            defaults=defaults,
        )
        if not created and (item.item_name != item_name or item.item_type != "Seed"):
            raise RuntimeError(
                f"Item {item_id} already exists with incompatible definition "
                f"{item.item_name}/{item.item_type}; no existing item was overwritten."
            )

    for plot_code in PLOT_CODES:
        FarmPlot.objects.using(alias).get_or_create(
            plot_code=plot_code,
            defaults={"status": "EMPTY"},
        )

    # Empty means "use the whole import whitelist".  Preserve that sentinel;
    # only extend deployments which opted into an explicit caravan item pool.
    for config in SimulationConfig.objects.using(alias).exclude(caravan_item_ids=[]):
        expanded = list(config.caravan_item_ids or [])
        for item_id in (29301, 29302, 29303):
            if item_id not in expanded:
                expanded.append(item_id)
        if expanded != config.caravan_item_ids:
            config.caravan_item_ids = expanded
            config.save(using=alias, update_fields=["caravan_item_ids"])


class Migration(migrations.Migration):
    dependencies = [
        ("items", "0005_alter_item_options"),
        ("village", "0040_retire_dialogue_commands"),
    ]
    operations = [
        # Reverse is intentionally a no-op: harvested assets and planted batches
        # may refer to these rows after deployment.
        migrations.RunPython(add_bill_farm_catalog, migrations.RunPython.noop),
    ]
