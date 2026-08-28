from django.db import migrations


COMMON_QUERIES = [
    "OBSERVE", "INSPECT_NEARBY", "INSPECT_INVENTORY",
    "INSPECT_TRADE_OFFERS", "INSPECT_STORY_STATE",
]

NPC_QUERIES = {
    "10011": ["INSPECT_ORDERS", "INSPECT_MARKET_STATE", "INSPECT_FARM_STATE"],
    "10012": ["INSPECT_ORDERS", "INSPECT_MARKET_STATE"],
    "10013": ["INSPECT_RATEABLE_DISHES", "INSPECT_PUBLIC_ECONOMY_STATE"],
    "10014": ["INSPECT_ORDERS", "INSPECT_MARKET_STATE", "INSPECT_RESTAURANT_STATE"],
    "10015": ["INSPECT_ORDERS", "INSPECT_MARKET_STATE"],
}


def append_query_capabilities(apps, schema_editor):
    NpcProfile = apps.get_model("village", "NpcProfile")
    for profile in NpcProfile.objects.select_related("npc"):
        npc_id = str(profile.npc.agent_id)
        if npc_id not in NPC_QUERIES:
            continue
        capabilities = list(profile.capabilities or [])
        for query in COMMON_QUERIES + NPC_QUERIES[npc_id]:
            if query not in capabilities:
                capabilities.append(query)
        if capabilities != profile.capabilities:
            profile.capabilities = capabilities
            profile.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0020_configure_npc_command_capabilities")]
    operations = [migrations.RunPython(append_query_capabilities, migrations.RunPython.noop)]
