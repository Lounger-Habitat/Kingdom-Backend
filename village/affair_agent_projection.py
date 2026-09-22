"""Actor-specific actions for system-created village affairs."""
from copy import deepcopy


def project_npc_affairs(affairs, capabilities, *, unavailable=False):
    can_act = "CONTRIBUTE_TO_AFFAIR" in {str(value).upper() for value in capabilities}
    result = deepcopy(affairs)
    for row in result:
        available = any(c.get("available") for s in row.get("stages", []) for c in s.get("conditions", []))
        row["available_actions"] = ["CONTRIBUTE_TO_AFFAIR"] if can_act and not unavailable and available else []
    return result
