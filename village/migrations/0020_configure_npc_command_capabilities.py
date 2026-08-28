from django.db import migrations


BASE_ACTIONS = [
    "MOVE", "WAIT", "GO_HOME", "EAT", "REST", "SLEEP", "SOCIALIZE",
    "WORK_PLACEHOLDER", "PROPOSE_TRADE", "RESPOND_TRADE",
    "CANCEL_TRADE_OFFER", "BUY_FROM_STORE", "INSPECT_STORE_INVENTORY",
    "INSPECT_SOCIAL_STATE", "CREATE_COMMITMENT", "CANCEL_COMMITMENT",
    "PERFORM_COMMITMENT", "STORY_VOTE", "STORY_ACT",
]

NPC_ACTIONS = {
    "10011": ["PLANT", "HARVEST", "CREATE_ORDER", "CANCEL_ORDER", "START_DELIVERY"],
    "10012": ["HUNT", "CREATE_ORDER", "CANCEL_ORDER", "START_DELIVERY"],
    "10013": ["RATE_DISH", "PUBLISH_PRICE_INDEX", "AUDIT_PUBLIC_FUND"],
    "10014": [
        "COOK", "CREATE_ORDER", "CANCEL_ORDER", "START_DELIVERY",
        "LIST_STORE_ITEM", "UPDATE_STORE_PRICE", "DELIST_STORE_ITEM",
    ],
    "10015": [
        "IMPORT_GOODS", "EXPORT_GOODS", "CREATE_ORDER", "CANCEL_ORDER",
        "START_DELIVERY", "LIST_STORE_ITEM", "UPDATE_STORE_PRICE",
        "DELIST_STORE_ITEM",
    ],
}


def configure_actions(apps, schema_editor):
    NpcProfile = apps.get_model("village", "NpcProfile")
    for profile in NpcProfile.objects.select_related("npc"):
        npc_id = str(profile.npc.agent_id)
        if npc_id in NPC_ACTIONS:
            profile.capabilities = BASE_ACTIONS + NPC_ACTIONS[npc_id]
            profile.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0019_npc_subjective_goal_state")]
    operations = [migrations.RunPython(configure_actions, migrations.RunPython.noop)]
