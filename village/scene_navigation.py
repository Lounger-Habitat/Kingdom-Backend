"""Agent movement intent validation. Physical reachability belongs to Unity."""
import math
from urllib.parse import unquote
from uuid import UUID


def valid_position(value):
    return (isinstance(value, dict) and set(value) == {"x", "y", "z"}
            and all(type(v) in (int, float) and abs(v) <= 100_000 and math.isfinite(v)
                    for v in value.values()))


def validate_move(params, sender):
    from inventory.models import NpcDefinition
    from .models import VillageLocationDefinition, VillageTreeNode, VillageWildlife
    if not isinstance(params, dict) or len(params) != 1:
        return "INVALID_COMMAND_SCHEMA"
    if "position" in params:
        return None if valid_position(params["position"]) else "INVALID_COMMAND_SCHEMA"
    if "location_code" in params:
        code = params["location_code"]
        # Preserve legacy admission: Unity resolves the authored location anchor.
        return None if isinstance(code, str) and 0 < len(code) <= 64 else "INVALID_LOCATION"
    if "target_npc_id" in params:
        target = params["target_npc_id"]
        return None if isinstance(target, str) and target != str(sender) and NpcDefinition.objects.filter(
            agent_id=target, is_test_only=False).exists() else "SOCIAL_TARGET_INVALID"
    ref = params.get("target_ref")
    if not isinstance(ref, str) or len(ref) > 256:
        return "INVALID_COMMAND_SCHEMA"
    kind, separator, key = ref.partition(":")
    if not separator or not key:
        return "INVALID_COMMAND_SCHEMA"
    if kind == "npc":
        return validate_move({"target_npc_id": key}, sender)
    if kind == "location":
        return None if VillageLocationDefinition.objects.filter(location_code=key, is_active=True).exists() else "INVALID_LOCATION"
    if kind == "player":
        from account.models import GameAccount
        return None if GameAccount.objects.filter(username=unquote(key), is_active=True).exists() else "TARGET_NOT_FOUND"
    if kind == "plot":
        from .models import VillageFarmPlot
        return None if VillageFarmPlot.objects.filter(plot_code=key).exists() else "TARGET_NOT_FOUND"
    try:
        if kind == "tree":
            identifier, generation = key.rsplit(":", 1)
            return None if VillageTreeNode.objects.filter(pk=UUID(identifier), generation=int(generation)).exists() else "TARGET_STALE"
        if kind == "animal":
            return None if VillageWildlife.objects.filter(pk=UUID(key)).exists() else "TARGET_NOT_FOUND"
    except (ValueError, TypeError, OverflowError):
        return "INVALID_COMMAND_SCHEMA"
    return "INVALID_COMMAND_SCHEMA"


def valid_nearby_options(params):
    if params is None:
        return True
    if not isinstance(params, dict) or set(params) - {"categories", "limit"}:
        return False
    limit = params.get("limit", 30)
    categories = params.get("categories", ["people", "trees", "animals", "facilities"])
    return (type(limit) is int and 1 <= limit <= 100 and isinstance(categories, list)
            and 1 <= len(categories) <= 4 and all(isinstance(c, str) for c in categories)
            and len(set(categories)) == len(categories)
            and set(categories) <= {"people", "trees", "animals", "facilities"})
