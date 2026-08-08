"""Phase-zero safety policy for formal village NPC assets."""

from django.conf import settings


FORMAL_VILLAGE_NPC_IDS = frozenset({"10011", "10012", "10013", "10014", "10015"})

LEGACY_WRITE_BLOCKED = "VILLAGE_NPC_LEGACY_WRITE_BLOCKED"
ASSET_ACTION_BLOCKED = "VILLAGE_NPC_ASSET_ACTION_BLOCKED"
TEST_ASSET_ACTIONS_DISABLED = "VILLAGE_TEST_ASSET_ACTIONS_DISABLED"
NOT_BOOTSTRAPPED = "VILLAGE_NPC_NOT_BOOTSTRAPPED"


def is_formal_village_npc(agent_id):
    return str(agent_id or "").strip() in FORMAL_VILLAGE_NPC_IDS


def test_asset_actions_enabled():
    return bool(settings.VILLAGE_TEST_ASSET_ACTIONS_ENABLED)


def blocked_response(result_code, message):
    return {
        "success": False,
        "message": message,
        "resultCode": result_code,
    }
