from django.db import migrations


def add_get_rule_capability(apps, schema_editor):
    profiles = apps.get_model("village", "NpcProfile").objects.using(schema_editor.connection.alias)
    for profile in profiles.filter(
        npc__agent_id__in=["10011", "10012", "10013", "10014", "10015"],
        npc__is_test_only=False, is_active=True,
    ):
        if "GET_RULE" not in profile.capabilities:
            profile.capabilities = [*profile.capabilities, "GET_RULE"]
            profile.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0030_npcdialoguesession_conversation_id_char")]
    operations = [migrations.RunPython(add_get_rule_capability, migrations.RunPython.noop)]
