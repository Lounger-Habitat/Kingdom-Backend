"""Keep survival queries available and retire unrewarded placeholder work."""
from django.db import migrations


def update_capabilities(apps, schema_editor):
    profiles = apps.get_model("village", "NpcProfile").objects.using(schema_editor.connection.alias)
    for profile in profiles.filter(
        npc__agent_id__in=["10011", "10012", "10013", "10014", "10015"],
        npc__is_test_only=False,
    ):
        actions = [action for action in (profile.capabilities or [])
                   if str(action).strip().upper() != "WORK_PLACEHOLDER"]
        for required in ("GET_RULE", "INSPECT_LIVING_OPTIONS"):
            if required not in {str(action).strip().upper() for action in actions}:
                actions.append(required)
        profile.capabilities = actions
        profile.save(using=schema_editor.connection.alias, update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0055_farm_plot_leasing")]
    operations = [migrations.RunPython(update_capabilities, migrations.RunPython.noop)]
