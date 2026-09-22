"""Remove NPC commitment tools, retaining relationships and historical records."""
from django.db import migrations


def update_capabilities(apps, schema_editor):
    retired = {"CREATE_COMMITMENT", "CANCEL_COMMITMENT", "PERFORM_COMMITMENT"}
    profiles = apps.get_model("village", "NpcProfile").objects.using(schema_editor.connection.alias)
    for profile in profiles.all():
        actions = [action for action in profile.capabilities or []
                   if str(action).strip().upper() not in retired]
        if actions != profile.capabilities:
            profile.capabilities = actions
            profile.save(using=schema_editor.connection.alias, update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0057_independent_affair_commands")]
    operations = [migrations.RunPython(update_capabilities, migrations.RunPython.noop)]
