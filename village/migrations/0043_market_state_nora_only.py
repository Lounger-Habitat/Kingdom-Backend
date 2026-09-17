from django.db import migrations


def restrict_market_state(apps, schema_editor):
    """Keep the caravan/foreign-trade query on Nora's capability list only."""
    NpcProfile = apps.get_model("village", "NpcProfile")
    alias = schema_editor.connection.alias
    action = "INSPECT_MARKET_STATE"
    for profile in NpcProfile.objects.using(alias).select_related("npc"):
        capabilities = [
            value for value in (profile.capabilities or [])
            if str(value).strip().upper() != action
        ]
        if str(profile.npc.agent_id) == "10015":
            # Preserve the existing order when Nora already has this capability.
            capabilities = list(profile.capabilities or [])
            if not any(str(value).strip().upper() == action for value in capabilities):
                capabilities.append(action)
        if capabilities != profile.capabilities:
            profile.capabilities = capabilities
            profile.save(using=alias, update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0042_villagestoreinventoryslotdish_and_more")]
    operations = [migrations.RunPython(restrict_market_state, migrations.RunPython.noop)]
