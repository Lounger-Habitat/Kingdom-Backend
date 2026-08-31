from django.db import migrations


def remove_inspect_orders_capability(apps, schema_editor):
    """Drop the phased-out INSPECT_ORDERS query from every NPC profile.

    The capability was already absent from the live DB for all formal NPCs,
    but seed_data.py is the source of truth on world reset. This migration
    guarantees existing environments converge without depending on a reseed.
    """
    NpcProfile = apps.get_model("village", "NpcProfile")
    for profile in NpcProfile.objects.all():
        capabilities = [
            action for action in (profile.capabilities or [])
            if str(action).strip().upper() != "INSPECT_ORDERS"
        ]
        if capabilities != profile.capabilities:
            profile.capabilities = capabilities
            profile.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0024_marketplace_npc_capabilities")]
    operations = [migrations.RunPython(
        remove_inspect_orders_capability,
        migrations.RunPython.noop,
    )]
