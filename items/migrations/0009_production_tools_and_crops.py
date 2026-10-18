"""G2 static definitions and future caravan catalog, never today's frozen visit."""
from django.db import migrations


def install(apps, schema_editor):
    alias = schema_editor.connection.alias
    Item = apps.get_model("items", "Item")
    Definition = apps.get_model("player", "ItemGameplayDefinition")
    Config = apps.get_model("village", "VillageSimulationConfig")
    definitions = {
        4102: ("改良采集工具包", "CollectTool", 150, 50, "targeted_gathering", 2, "gathering", 4),
        4103: ("改良农具", "HoeTool", 150, 50, "farming", 2, "farming", 4),
        4104: ("基础斧", "ChopTool", 50, 20, "forestry", 1, "forestry", 1),
        4105: ("改良斧", "ChopTool", 150, 50, "forestry", 2, "forestry", 4),
    }
    for key, (name, kind, price, uses, capability, tier, skill, level) in definitions.items():
        item, _ = Item.objects.using(alias).get_or_create(item_id=key, defaults=dict(item_name=name,
            item_type=kind, item_price=price, max_use_count=uses, collectable=False, stackable=False))
        if item.item_name != name or item.item_type != kind:
            raise ValueError(f"G2 item ID collision: {key}")
        Definition.objects.using(alias).get_or_create(item_id=key, defaults=dict(capability_code=capability,
            tier=tier, required_skill=skill, required_level=level,
            effects=[dict(code="production_tool", uses_per_success=1)]))
    farm, _ = Item.objects.using(alias).get_or_create(item_id=4001, defaults=dict(item_name="阶段三农具",
        item_type="HoeTool", item_price=50, max_use_count=20, collectable=False, stackable=False))
    Definition.objects.using(alias).get_or_create(item_id=farm.pk, defaults=dict(capability_code="farming",
        tier=1, required_skill="farming", required_level=1))
    for key, name, kind, price in [(29304, "洋葱种子", "Seed", 5), (29305, "青萝卜种子", "Seed", 8),
                                  (29306, "硬木", "Commodity", 12)]:
        item, _ = Item.objects.using(alias).get_or_create(item_id=key, defaults=dict(item_name=name,
            item_type=kind, item_price=price, collectable=False, stackable=True))
        if item.item_name != name or item.item_type != kind:
            raise ValueError(f"G2 item ID collision: {key}")
    for cfg in Config.objects.using(alias).all():
        # An explicitly disabled catalog stays disabled. Existing frozen visit
        # and demand records are not regenerated or re-priced.
        fields = []
        if cfg.caravan_item_ids:
            cfg.caravan_item_ids = sorted(set(cfg.caravan_item_ids) | {29304, 29305})
            fields.append("caravan_item_ids")
        if cfg.caravan_export_item_ids:
            cfg.caravan_export_item_ids = sorted(set(cfg.caravan_export_item_ids) | {1038, 1043, 29306})
            fields.append("caravan_export_item_ids")
        if fields:
            cfg.save(using=alias, update_fields=fields)


class Migration(migrations.Migration):
    dependencies = [("items", "0008_gathering_tool"), ("player", "0015_starter_supply"),
                    ("village", "0076_production_growth")]
    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
