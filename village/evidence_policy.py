"""Evidence freshness owned by the village, separate from tool permissions.

Selectors use {self} or an action parameter; an omitted selector denotes the
whole collection. Unknown tools deliberately keep the agent's conservative
fallback. These declarations describe state dependencies, never authorization.
"""

from copy import deepcopy


def _read(*domains, seconds=60, **extra):
    return dict(reads=list(domains), writes=[], max_age_seconds=seconds, **extra)


def _write(*domains, **extra):
    return dict(reads=[], writes=list(domains), event_fields=[
        "action", "receipt_id", "world_time", "items_consumed", "gold_amount",
        "exchange_id", "target_type", "target_id", "condition_id", "affair_id",
        "actual_duration_minutes",
    ], **extra)


POLICIES = {
    "INSPECT_INVENTORY": _read("inventory:{self}", "wallet:{self}", snapshots=[
        {"domains": ["inventory:{self}"], "fields": ["items", "total_count", "has_more", "filter", "tool_summaries"]},
        {"domains": ["wallet:{self}"], "fields": ["money", "available_money"]},
    ]),
    "INSPECT_NEARBY": _read("position:{self}", "nearby:*", seconds=10),
    "INSPECT_AFFAIRS": _read("affair:{affair_id}", seconds=30, omit_fields=["available_actions"]),
    "OBSERVE": _read("position:{self}", "needs:{self}", "activity:{self}", seconds=30, snapshots=[
        {"domains": ["position:{self}"], "fields": ["self_npc_id", "self_name", "current_location", "current_point_code", "known_locations"]},
        {"domains": ["needs:{self}"], "fields": ["needs", "need_bands", "today_sleep_minutes", "continuous_sleep_minutes", "today_longest_continuous_sleep_minutes", "previous_core_night_sleep_minutes", "previous_core_night_penalized"], "max_age_world_minutes": 1},
        {"domains": ["activity:{self}"], "fields": ["current_action", "action_status", "active_command_id", "interruptible", "sleep", "remaining_minutes"], "max_age_world_minutes": 1},
    ]),
    "INSPECT_SCHEDULE": _read("schedule:{self}:{game_date}"),
    "INSPECT_DIARIES": _read("diary:{self}:{game_date}"),
    "WRITE_SCHEDULE": _write("schedule:{self}:{game_date}"),
    "UPDATE_SCHEDULE": _write("schedule:{self}:{game_date}"),
    "WRITE_DIARY": _write("diary:{self}:{game_date}"),
    "UPDATE_DIARY": _write("diary:{self}:{game_date}"),
    "EAT": _write("inventory:{self}", "needs:{self}", "activity:{self}", snapshots=[
        {"domains": ["needs:{self}"], "fields": ["needs_after"], "max_age_world_minutes": 1},
    ]),
    "MOVE": _write("position:{self}", "nearby:*", "needs:{self}", "activity:{self}"),
    "GO_HOME": _write("position:{self}", "nearby:*", "needs:{self}", "activity:{self}"),
    "REST": _write("needs:{self}", "activity:{self}"),
    "SLEEP": _write("needs:{self}", "activity:{self}"),
    "WAKE_UP": _write("needs:{self}", "activity:{self}"),
    "GIVE_ITEMS": _write("inventory:{self}", "wallet:{self}", "inventory:{target_id}", "wallet:{target_id}"),
    "CONTRIBUTE_TO_AFFAIR": _write("affair:{affair_id}", "inventory:{self}", "wallet:{self}", "needs:{self}", "activity:{self}"),
}


def policy_for(action):
    return deepcopy(POLICIES.get(action))


def event_changes(content):
    """Only use fields already visible to this recipient; include no balances."""
    kind = str(content.get("event_type") or "").upper()
    public = content.get("public_data") or {}
    if kind.startswith("VILLAGE_AFFAIR_") and public.get("affair_id"):
        return ["affair:" + str(public["affair_id"])]
    if kind in {"PLAYER_GIFT", "PLAYER_RESCUE_GIFT", "ECONOMY_GIVE_ITEMS",
                "ECONOMY_BUY_FROM_STORE", "ECONOMY_PROPOSE_TRADE",
                "ECONOMY_RESPOND_TRADE", "ECONOMY_CANCEL_TRADE_OFFER", "MARK_DAILY_SALARY"}:
        actors = set(content.get("participant_npc_ids") or [])
        if content.get("actor_npc_id"):
            actors.add(content["actor_npc_id"])
        return [f"{domain}:{actor}" for actor in sorted(actors)
                for domain in ("inventory", "wallet")] or ["*"]
    return ["*"]
