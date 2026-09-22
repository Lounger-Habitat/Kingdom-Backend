from django.db import migrations


def seed(apps, schema_editor):
    alias = schema_editor.connection.alias
    Duration = apps.get_model("village", "VillageActionDurationConfig")
    Duration.objects.using(alias).get_or_create(action_name="CHOP_TREE", defaults={"duration_minutes": 120})
    Location = apps.get_model("village", "VillageLocationDefinition")
    Location.objects.using(alias).get_or_create(location_code="FOREST_LOGGING", defaults=dict(
        display_name="西侧伐木林", center_anchor_code="LOC_FOREST_LOGGING", location_type="resource",
        bounds={"center": [-118, 2.42818, 43]}, opening_hours={"alwaysOpen": True},
        supported_action_types=["CHOP_TREE"], capacity=8, is_public=True, is_active=True))
    Profile = apps.get_model("village", "NpcProfile")
    for profile in Profile.objects.using(alias).filter(npc__agent_id="10012"):
        if "CHOP_TREE" not in profile.capabilities:
            profile.capabilities = [*profile.capabilities, "CHOP_TREE"]
            profile.save(using=alias, update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0064_forestry")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
