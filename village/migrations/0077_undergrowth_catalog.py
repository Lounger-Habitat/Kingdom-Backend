from django.db import migrations


def install(apps, schema_editor):
    alias = schema_editor.connection.alias
    Point = apps.get_model("inventory", "CollectionPoint")
    Level = apps.get_model("inventory", "CollectionPointLevel")
    Entry = apps.get_model("inventory", "CollectionPointLevelItem")
    Item = apps.get_model("items", "Item")
    Location = apps.get_model("village", "VillageLocationDefinition")
    point, _ = Point.objects.using(alias).get_or_create(collect_type="采集林下食材", defaults=dict(
        name="林下食材点", item_type="食材", action_cost=20, collect_count=2, default_level=1))
    level, _ = Level.objects.using(alias).get_or_create(collection_point=point, level=1)
    for item_id in (1035, 1036, 1037, 1038, 1073):
        if Item.objects.using(alias).filter(pk=item_id, is_deleted=False).exists():
            Entry.objects.using(alias).get_or_create(level=level, item_id=item_id,
                defaults=dict(collect_weight=100, collect_min_amount=2, collect_max_amount=4))
    Location.objects.using(alias).get_or_create(location_code="FOREST_UNDERGROWTH", defaults=dict(
        display_name="林下食材点", center_anchor_code="FOREST_UNDERGROWTH", location_type="resource",
        bounds={"center": {"x": -110, "y": 2.4, "z": 52}, "radius": 4},
        location_tags=["gathering", "advanced"], supported_action_types=["COLLECT"], capacity=4))


class Migration(migrations.Migration):
    dependencies = [("village", "0076_production_growth"), ("items", "0008_gathering_tool")]
    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
