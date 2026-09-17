from django.db import migrations


def remove_dialogue_commands(apps, schema_editor):
    """Remove retired action permissions; event speech uses the shared session service."""
    Profile = apps.get_model("village", "NpcProfile")
    alias = schema_editor.connection.alias
    retired = {"START_DIALOGUE", "SPEAK"}
    for profile in Profile.objects.using(alias).all():
        previous = profile.capabilities or []
        current = [value for value in previous if str(value).strip().upper() not in retired]
        if current != previous:
            profile.capabilities = current
            profile.save(using=alias, update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0039_lucy_catalog_permissions")]
    operations = [migrations.RunPython(remove_dialogue_commands, migrations.RunPython.noop)]
