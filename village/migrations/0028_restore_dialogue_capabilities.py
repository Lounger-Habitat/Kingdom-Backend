from django.db import migrations


def restore_dialogue_capabilities(apps, schema_editor):
    profiles = apps.get_model("village", "NpcProfile").objects.using(schema_editor.connection.alias)
    for profile in profiles.filter(
        npc__agent_id__in=["10011", "10012", "10013", "10014", "10015"],
        npc__is_test_only=False, is_active=True,
    ):
        actions = [action for action in profile.capabilities if action != "SOCIALIZE"]
        for action in ("START_DIALOGUE", "SPEAK"):
            if action not in actions:
                actions.append(action)
        if actions != profile.capabilities:
            profile.capabilities = actions
            profile.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0027_villagestoreinventoryslot_remaining_uses")]
    operations = [migrations.RunPython(restore_dialogue_capabilities, migrations.RunPython.noop)]
