"""Bounded read-only projections for Phase 7 player and operator views."""

from __future__ import annotations

from django.db import connection
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone

from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot

from .models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    NpcActionReservation,
    NpcCommandState,
    NpcCommitment,
    NpcDialogueSession,
    NpcProfile,
    NpcRelationship,
    NpcRuntimeState,
    NpcSubjectiveGoalState,
    NpcStoryFactAccess,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageActionReceipt,
    VillageDeliveryCursor,
    VillageDeliveryOutbox,
    VillageLocationDefinition,
    VillageStoryArc,
    VillageStoryFact,
    VillageStoryTransitionReceipt,
    VillageWorldEvent,
)
from .phase3_catalog import TOOL_ITEM_IDS
from .phase4_contract import relationship_label, relationship_snapshot


FORMAL_NPC_IDS = ("10011", "10012", "10013", "10014", "10015")
PRICE_ITEM_IDS = (1030, 1056, 1053, 1067)
FOOD_ITEM_IDS = (*PRICE_ITEM_IDS, 29001)
SCAN_BATCH_SIZE = 200
SCAN_EVENT_BUDGET = 1000


def presentation_enabled() -> bool:
    from .models import VillageSimulationConfig

    return VillageSimulationConfig.objects.filter(
        config_key="default", village_presentation_enabled=True
    ).exists()


def _category(event: VillageWorldEvent) -> str:
    explicit = str((event.public_data or {}).get("category", "")).upper()
    if explicit:
        return explicit
    event_type = event.event_type.upper()
    if event_type.startswith("STORY_"):
        return "STORY"
    if any(word in event_type for word in (
        "TRADE", "ORDER", "STORE", "PRICE", "FUND", "IMPORT", "EXPORT",
        "COOK", "HARVEST", "HUNT", "PLANT",
    )):
        return "ECONOMY"
    return "NPC"


def _visible(event: VillageWorldEvent, player_id: str) -> bool:
    return event.visibility == "PUBLIC" or player_id in {
        str(value) for value in (event.player_ids or [])
    }


def _journal_item(event: VillageWorldEvent) -> dict:
    return {
        "event_id": str(event.event_id),
        "event_sequence": event.event_sequence,
        "event_type": event.event_type,
        "world_minute": event.world_minute,
        "actor_npc_id": str(event.actor_npc.agent_id) if event.actor_npc else None,
        "participant_npc_ids": [str(value) for value in (event.participant_npc_ids or [])],
        "location_code": event.location_code,
        "player_summary": event.player_summary,
        "category": _category(event),
    }


def public_journal(player_id: str, filters: dict) -> dict:
    """Return a private-safe reverse cursor page without unbounded materialization."""
    limit = filters["limit"]
    scan_before = filters.get("before_sequence")
    events = []
    scanned = 0
    reached_end = False
    while len(events) < limit and scanned < SCAN_EVENT_BUDGET:
        queryset = VillageWorldEvent.objects.select_related("actor_npc")
        if connection.features.supports_json_field_contains:
            visible = Q(visibility="PUBLIC") | Q(player_ids__contains=[player_id])
            if player_id.isdigit():
                visible |= Q(player_ids__contains=[int(player_id)])
            queryset = queryset.filter(visible)
        if scan_before is not None:
            queryset = queryset.filter(event_sequence__lt=scan_before)
        if filters.get("day_index") is not None:
            start = filters["day_index"] * 1440
            queryset = queryset.filter(world_minute__gte=start, world_minute__lt=start + 1440)
        if filters.get("location_code"):
            queryset = queryset.filter(location_code=filters["location_code"])
        batch_limit = min(SCAN_BATCH_SIZE, SCAN_EVENT_BUDGET - scanned)
        batch = list(queryset.order_by("-event_sequence")[:batch_limit])
        if not batch:
            reached_end = True
            break
        scanned += len(batch)
        for event in batch:
            scan_before = event.event_sequence
            if not _visible(event, player_id):
                continue
            npc_id = filters.get("npc_id")
            if npc_id and npc_id not in {
                str(event.actor_npc.agent_id) if event.actor_npc else "",
                *(str(value) for value in (event.participant_npc_ids or [])),
            }:
                continue
            if filters.get("category") and _category(event) != filters["category"]:
                continue
            events.append(_journal_item(event))
            if len(events) == limit:
                break
        if len(batch) < batch_limit:
            reached_end = True
            break
    next_sequence = None
    if len(events) == limit:
        next_sequence = events[-1]["event_sequence"]
    elif not reached_end and scanned >= SCAN_EVENT_BUDGET:
        next_sequence = scan_before
    return {
        "events": events,
        "next_before_sequence": next_sequence,
    }


def _coverage_days() -> tuple[float, float]:
    slots = NpcGlobalInventorySlot.objects.filter(
        bag__npc__agent_id__in=FORMAL_NPC_IDS, item_amount__gt=0
    )
    tool_uses = sum(
        row.remaining_uses if row.remaining_uses is not None else row.item_amount
        for row in slots.filter(item_id__in=TOOL_ITEM_IDS)
    )
    food_amount = slots.filter(item_id__in=FOOD_ITEM_IDS).aggregate(total=Sum("item_amount"))["total"] or 0
    return round(food_amount / 5, 2), round(tool_uses / 2, 2)


def _fund_delta(entry: EconomicLedgerEntry) -> int:
    value = 0
    public_fund = (entry.changes or {}).get("PUBLIC_FUND")
    if isinstance(public_fund, dict):
        try:
            value += int(public_fund.get("money", 0) or 0)
        except (TypeError, ValueError):
            pass
    if entry.entry_type in {"IMPORT_GOODS", "EXPORT_GOODS"}:
        value += int(entry.external_delta or 0)
    return value


def _public_economy(world_minute: int) -> dict:
    day_index = world_minute // 1440
    food_days, tool_days = _coverage_days()
    prices = [
        {
            "day_index": row.day_index,
            "prices": {
                str(item_id): (row.prices or {}).get(str(item_id))
                for item_id in PRICE_ITEM_IDS
                if str(item_id) in (row.prices or {})
            },
        }
        for row in DailyPriceSnapshot.objects.filter(status="PUBLISHED")
        .order_by("-day_index")[:7]
    ]
    today = EconomicLedgerEntry.objects.filter(
        world_minute__gte=day_index * 1440,
        world_minute__lt=(day_index + 1) * 1440,
    )
    import_total = abs(today.filter(entry_type="IMPORT_GOODS").aggregate(total=Sum("external_delta"))["total"] or 0)
    export_total = today.filter(entry_type="EXPORT_GOODS").aggregate(total=Sum("external_delta"))["total"] or 0
    uses = []
    for entry in EconomicLedgerEntry.objects.order_by("-world_minute", "-id")[:100]:
        delta = _fund_delta(entry)
        if delta < 0:
            uses.append({
                "entry_type": entry.entry_type,
                "world_minute": entry.world_minute,
                "amount": abs(delta),
            })
        if len(uses) == 10:
            break
    orders = [
        {
            "order_id": str(row.order_id),
            "item_id": row.item_id,
            "amount": row.amount,
            "unit_price": row.unit_price,
            "delivery_point_code": row.delivery_point_code,
            "expires_world_minute": row.expires_world_minute,
        }
        for row in NpcTradeOrder.objects.filter(side="BUY", status="OPEN")
        .order_by("created_at", "order_id")[:10]
    ]
    fund = VillageAccount.objects.filter(account_code="PUBLIC_FUND").values_list("balance", flat=True).first() or 0
    return {
        "food_stock_coverage_days": food_days,
        "tool_stock_coverage_days": tool_days,
        "price_history": prices,
        "today_import_total": import_total,
        "today_export_total": export_total,
        "public_fund_balance": fund,
        "recent_public_fund_uses": uses,
        "public_orders": orders,
    }


def _public_story(player_id: str) -> dict:
    arc = VillageStoryArc.objects.order_by("arc_code").first()
    if not arc:
        return {}
    fact = getattr(arc, "facts", None)
    progress = {}
    if fact is not None and arc.current_plan == "BRIDGE_REPAIR":
        progress = {
            "bridge_condition": fact.bridge_condition,
            "repair_fund": fact.repair_fund,
            "repair_wood": fact.repair_wood,
            "repair_metal": fact.repair_metal,
            "repair_labor_hours": fact.repair_labor_hours,
            "community_meals": fact.community_meals,
        }
    elif fact is not None and arc.current_plan == "FOREST_ROUTE":
        progress = {
            "forest_route_scouted": fact.forest_route_scouted,
            "forest_route_supply": fact.forest_route_supply,
        }
        if fact.emergency_caravan_contacted:
            progress["emergency_caravan_status"] = (
                "SUCCEEDED" if fact.emergency_caravan_succeeded else "FAILED"
            )
    elif fact is not None and arc.current_plan == "RATION_AND_WAIT":
        progress = {
            "ration_rules_published": fact.ration_rules_published,
            "store_limits_enabled": fact.store_limits_enabled,
            "community_meals": fact.community_meals,
        }
        if fact.store_limits_enabled:
            progress["ration_daily_limits"] = dict(fact.ration_daily_limits or {})
    transition = VillageStoryTransitionReceipt.objects.filter(
        arc=arc, applied=True
    ).order_by("-created_at").first()
    contribution_events = []
    for event in VillageWorldEvent.objects.filter(event_type__startswith="STORY_").order_by("-event_sequence")[:100]:
        if player_id in {str(value) for value in (event.player_ids or [])}:
            contribution_events.append(str(event.event_id))
            if len(contribution_events) == 10:
                break
    result = {
        "arc_code": arc.arc_code,
        "public_problem": arc.current_phase,
        "public_plan": arc.current_plan or None,
        "progress": progress,
        "player_contribution": {
            "event_count": len(contribution_events),
            "event_ids": contribution_events,
        },
        "recent_transition_reason": (
            transition.world_event.player_summary if transition else ""
        ),
    }
    if arc.is_completed:
        result["outcome"] = arc.outcome
    return result


def _location_is_open(opening_hours: dict, world_minute: int) -> bool:
    hours = opening_hours if isinstance(opening_hours, dict) else {}
    if hours.get("alwaysOpen") is True or not hours:
        return True
    if hours.get("alwaysOpen") is not False:
        return False
    open_minute = hours.get("openMinute")
    close_minute = hours.get("closeMinute")
    if (
        isinstance(open_minute, bool) or not isinstance(open_minute, int)
        or isinstance(close_minute, bool) or not isinstance(close_minute, int)
        or not 0 <= open_minute < 1440
        or not 0 <= close_minute < 1440
        or open_minute == close_minute
    ):
        return False
    minute_of_day = world_minute % 1440
    if open_minute < close_minute:
        return open_minute <= minute_of_day < close_minute
    return minute_of_day >= open_minute or minute_of_day < close_minute


def public_overview(player_id: str) -> dict:
    time_state = GlobalGameTimeState.objects.filter(pk=1).first()
    world_minute = time_state.world_minute if time_state else 0
    yesterday = max(0, world_minute // 1440 - 1)
    yesterday_events = public_journal(player_id, {"limit": 100, "day_index": yesterday})["events"]
    per_npc = {}
    for event in yesterday_events:
        npc_id = event["actor_npc_id"]
        if npc_id and npc_id not in per_npc:
            per_npc[npc_id] = {
                "npc_id": npc_id,
                "event_type": event["event_type"],
                "player_summary": event["player_summary"],
            }
    locations = [
        {
            "location_code": row.location_code,
            "display_name": row.display_name,
            "location_type": row.location_type,
            "is_open": _location_is_open(row.opening_hours, world_minute),
        }
        for row in VillageLocationDefinition.objects.filter(is_active=True, is_public=True)
        .order_by("location_code")[:100]
    ]
    return {
        "yesterday_summary": {
            "day_index": yesterday,
            "npc_actions": [
                per_npc.get(npc_id, {
                    "npc_id": npc_id,
                    "event_type": None,
                    "player_summary": "",
                })
                for npc_id in FORMAL_NPC_IDS
            ],
            "event_count": len(yesterday_events),
        },
        "economy": _public_economy(world_minute),
        "story": _public_story(player_id),
        "locations": locations,
    }


def public_npc_observation(npc_id: str) -> dict | None:
    profile = NpcProfile.objects.select_related("npc").filter(
        npc__agent_id=npc_id, npc__is_test_only=False, is_active=True
    ).first()
    if profile is None:
        return None
    runtime = NpcRuntimeState.objects.filter(npc=profile.npc).first()
    if runtime is None:
        return None
    goal = NpcSubjectiveGoalState.objects.filter(
        npc=profile.npc, status="ACTIVE", visibility="PUBLIC"
    ).first()
    names = {
        str(row.npc.agent_id): row.display_name
        for row in NpcProfile.objects.select_related("npc").filter(is_active=True)
    }
    open_commitments = NpcCommitment.objects.filter(status="OPEN").filter(
        Q(promisor_npc=profile.npc) | Q(beneficiary_npc=profile.npc)
    ).select_related("promisor_npc", "beneficiary_npc").order_by("due_world_minute")[:20]
    commitment_rows = list(open_commitments)
    relationships = []
    for row in NpcRelationship.objects.filter(from_npc=profile.npc).select_related("to_npc"):
        other_id = str(row.to_npc.agent_id)
        has_open = any(
            {item.promisor_npc_id, item.beneficiary_npc_id} == {profile.npc_id, row.to_npc_id}
            for item in commitment_rows
        )
        relationships.append({
            "other_npc_id": other_id,
            "other_display_name": names.get(other_id, row.to_npc.name),
            "label": relationship_label(relationship_snapshot(row), has_open),
            "has_open_commitment": has_open,
        })
    recent_commands = [
        {
            "name": row.kind,
            "status": row.status,
            "result_code": row.result_code,
            "completed_world_minute": row.completed_world_minute,
        }
        for row in NpcCommandState.objects.filter(
            npc=profile.npc, status__in=("SUCCEEDED", "FAILED")
        ).order_by("-completed_world_minute", "-updated_at")[:3]
    ]
    dialogue = NpcDialogueSession.objects.filter(
        Q(initiator=profile.npc) | Q(target=profile.npc), status="ACTIVE"
    ).first()
    return {
        "npc_id": npc_id,
        "display_name": profile.display_name,
        "public_needs": {
            "hunger": runtime.hunger_fp / 10,
            "energy": runtime.energy_fp / 10,
            "health": runtime.health_fp / 10,
            "social": runtime.social_fp / 10,
            "stress": runtime.stress_fp / 10,
        },
        "public_status": {
            "action": "SOCIALIZING" if dialogue else runtime.current_action or "IDLE",
            "action_status": "SOCIAL" if dialogue else runtime.action_status,
        },
        "subjective_goal": ({
            "goal_id": str(goal.goal_id),
            "summary": goal.summary,
            "status": goal.status,
            "goal_version": goal.goal_version,
            "updated_world_minute": goal.updated_world_minute,
        } if goal else None),
        "recent_commands": recent_commands,
        "relationships": relationships,
        "commitments": [
            {
                "commitment_id": str(row.commitment_id),
                "commitment_type": row.commitment_type,
                "promisor_npc_id": str(row.promisor_npc.agent_id),
                "promisor_display_name": names.get(str(row.promisor_npc.agent_id), row.promisor_npc.name),
                "beneficiary_npc_id": str(row.beneficiary_npc.agent_id),
                "beneficiary_display_name": names.get(str(row.beneficiary_npc.agent_id), row.beneficiary_npc.name),
                "status": row.status,
                "due_world_minute": row.due_world_minute,
            }
            for row in commitment_rows
        ],
    }


def operations_snapshot() -> dict:
    now = timezone.now()
    time_state = GlobalGameTimeState.objects.filter(pk=1).first()
    runtimes = list(
        NpcRuntimeState.objects.select_related("npc").filter(
            npc__agent_id__in=FORMAL_NPC_IDS
        ).order_by("npc__agent_id")[:100]
    )
    dialogue_npc_ids = set()
    for dialogue in NpcDialogueSession.objects.filter(status="ACTIVE").values_list(
        "initiator__agent_id", "target__agent_id"
    ):
        dialogue_npc_ids.update(str(npc_id) for npc_id in dialogue)
    recent = {
        str(runtime.npc.agent_id): [
            {
                "request_id": str(row.request_id),
                "result_code": row.result_code,
                "success": row.success,
                "world_minute": row.world_minute,
            }
            for row in VillageActionReceipt.objects.filter(npc=runtime.npc)
            .order_by("-created_at")[:5]
        ]
        for runtime in runtimes
    }
    npcs = [{
        "npc_id": str(row.npc.agent_id),
        "location_code": row.current_location_code,
        "action_status": "DIALOGUING" if str(row.npc.agent_id) in dialogue_npc_ids else row.action_status,
        "current_goal": row.current_goal,
        "current_action": "DIALOGUE" if str(row.npc.agent_id) in dialogue_npc_ids else row.current_action,
        "action_started_world_minute": row.action_started_world_minute,
        "action_ends_world_minute": row.action_ends_world_minute,
        "active_command_id": str(row.active_command_id) if row.active_command_id else None,
        "state_version": row.state_version,
        "time_revision": row.time_revision,
        "needs_world_minute": row.needs_world_minute,
        "needs": {
            "hunger": row.hunger_fp / 10,
            "energy": row.energy_fp / 10,
            "health": row.health_fp / 10,
            "social": row.social_fp / 10,
            "stress": row.stress_fp / 10,
        },
        "recent_results": recent[str(row.npc.agent_id)],
    } for row in runtimes]
    inventories = [{
        "npc_id": str(row.npc.agent_id),
        "money": row.money,
        "asset_version": row.asset_version,
        "items": [
            {
                "instance_id": str(slot.instance_id),
                "item_id": slot.item_id,
                "amount": slot.item_amount,
                "remaining_uses": slot.remaining_uses,
            }
            for slot in row.slots.order_by("slot_index")[:100]
        ],
    } for row in NpcGlobalInventory.objects.select_related("npc").filter(
        npc__agent_id__in=FORMAL_NPC_IDS
    ).order_by("npc__agent_id")[:100]]
    accounts = [{
        "account_code": row.account_code,
        "balance": row.balance,
        "asset_version": row.asset_version,
        "last_caravan_day": row.last_caravan_day,
        "last_closed_day_index": row.last_closed_day_index,
    } for row in VillageAccount.objects.order_by("account_code")[:100]]
    ledger_entries = [{
        "request_id": str(row.request_id),
        "entry_type": row.entry_type,
        "actor_npc_id": str(row.actor_npc.agent_id) if row.actor_npc else None,
        "changes": dict(row.changes or {}),
        "external_delta": row.external_delta,
        "world_minute": row.world_minute,
        "event_id": str(row.event.event_id) if row.event else None,
    } for row in EconomicLedgerEntry.objects.select_related(
        "actor_npc", "event"
    ).order_by("-created_at")[:100]]
    orders = [{
        "order_id": str(row.order_id),
        "request_id": str(row.request_id),
        "npc_id": str(row.npc.agent_id),
        "side": row.side,
        "item_id": row.item_id,
        "amount": row.amount,
        "unit_price": row.unit_price,
        "delivery_point_code": row.delivery_point_code,
        "expires_world_minute": row.expires_world_minute,
        "status": row.status,
        "matched_order_id": str(row.matched_order_id) if row.matched_order_id else None,
    } for row in NpcTradeOrder.objects.select_related("npc").order_by("-created_at")[:100]]
    deliveries = [{
        "delivery_id": str(row.delivery_id),
        "buyer_order_id": str(row.buyer_order_id),
        "seller_order_id": str(row.seller_order_id),
        "seller_slot_instance_id": str(row.seller_slot_instance_id),
        "reserved_amount": row.reserved_amount,
        "reserved_money": row.reserved_money,
        "delivery_point_code": row.delivery_point_code,
        "expires_world_minute": row.expires_world_minute,
        "status": row.status,
    } for row in NpcTradeDelivery.objects.order_by("-created_at")[:100]]
    relationships = [{
        "from_npc_id": str(row.from_npc.agent_id),
        "to_npc_id": str(row.to_npc.agent_id),
        "affinity": row.affinity,
        "trust": row.trust,
        "obligation": row.obligation,
        "conflict": row.conflict,
        "version": row.version,
        "last_meaningful_event_id": (
            str(row.last_meaningful_event.event_id) if row.last_meaningful_event else None
        ),
    } for row in NpcRelationship.objects.select_related(
        "from_npc", "to_npc", "last_meaningful_event"
    ).order_by("from_npc__agent_id", "to_npc__agent_id")[:100]]
    commitments = [{
        "commitment_id": str(row.commitment_id),
        "commitment_type": row.commitment_type,
        "promisor_npc_id": str(row.promisor_npc.agent_id),
        "beneficiary_npc_id": str(row.beneficiary_npc.agent_id),
        "target": dict(row.target or {}),
        "due_world_minute": row.due_world_minute,
        "status": row.status,
        "created_event_id": str(row.created_event.event_id),
        "settled_event_id": str(row.settled_event.event_id) if row.settled_event else None,
    } for row in NpcCommitment.objects.select_related(
        "promisor_npc", "beneficiary_npc", "created_event", "settled_event"
    ).order_by("-created_at")[:100]]
    fact_access = [{
        "npc_id": str(row.npc.agent_id),
        "fact_code": row.fact_code,
        "unlocked_world_minute": row.unlocked_world_minute,
        "is_public": row.is_public,
        "unlocked_by_event_id": (
            str(row.unlocked_by_event.event_id) if row.unlocked_by_event else None
        ),
    } for row in NpcStoryFactAccess.objects.select_related(
        "npc", "unlocked_by_event"
    ).order_by("npc__agent_id", "fact_code")[:100]]
    arcs = [{
        "arc_code": row.arc_code,
        "current_phase": row.current_phase,
        "current_plan": row.current_plan,
        "story_version": row.story_version,
        "is_completed": row.is_completed,
        "outcome": row.outcome,
        "start_day": row.start_day,
        "phase_entered_day": row.phase_entered_day,
        "last_crisis_day": row.last_crisis_day,
        "ration_until_day": row.ration_until_day,
        "review_count": row.review_count,
    } for row in VillageStoryArc.objects.order_by("arc_code")[:100]]
    story_facts = [{
        "arc_code": row.arc_id,
        "bridge_condition": row.bridge_condition,
        "tool_stock_coverage_days": float(row.tool_stock_coverage_days),
        "food_stock_coverage_days": float(row.food_stock_coverage_days),
        "repair_fund": row.repair_fund,
        "repair_wood": row.repair_wood,
        "repair_metal": row.repair_metal,
        "repair_labor_hours": row.repair_labor_hours,
        "public_trust": row.public_trust,
        "forest_route_known": row.forest_route_known,
        "forest_route_scouted": row.forest_route_scouted,
        "forest_route_supply": row.forest_route_supply,
        "emergency_caravan_contacted": row.emergency_caravan_contacted,
        "emergency_caravan_succeeded": row.emergency_caravan_succeeded,
        "bill_warning_revealed": row.bill_warning_revealed,
        "mark_budget_delay_revealed": row.mark_budget_delay_revealed,
        "ration_rules_published": row.ration_rules_published,
        "store_limits_enabled": row.store_limits_enabled,
        "ration_daily_limits": dict(row.ration_daily_limits or {}),
        "community_meals": row.community_meals,
        "last_progress_day": row.last_progress_day,
        "stagnant_days": row.stagnant_days,
    } for row in VillageStoryFact.objects.order_by("arc_id")[:100]]
    transitions = [{
        "request_id": str(row.request_id),
        "arc_code": row.arc_id,
        "from_phase": row.from_phase,
        "to_phase": row.to_phase,
        "selected_plan": row.selected_plan,
        "source_day": row.source_day,
        "event_id": str(row.world_event.event_id),
        "applied": row.applied,
        "response_data": dict(row.response_data or {}),
    } for row in VillageStoryTransitionReceipt.objects.select_related(
        "world_event"
    ).order_by("-created_at")[:100]]
    commands = [{
        "command_id": str(row.command_id),
        "npc_id": str(row.npc.agent_id),
        "kind": row.kind,
        "target_id": row.target_id,
        "location_code": row.location_code,
        "parameters": dict(row.sanitized_parameters or {}),
        "status": row.status,
        "result_code": row.result_code,
        "received_world_minute": row.received_world_minute,
    } for row in NpcCommandState.objects.select_related("npc").order_by("-created_at")[:100]]
    locks = [{
        "reservation_id": str(row.reservation_id),
        "action_kind": row.action_kind,
        "participant_npc_ids": [str(npc.agent_id) for npc in row.participants.all()],
        "resource_key": row.resource_key,
        "status": row.status,
        "expires_world_minute": row.expires_world_minute,
    } for row in NpcActionReservation.objects.prefetch_related("participants").order_by("-created_at")[:100]]
    counts = {
        f"{row['message_kind'].lower()}_{row['status'].lower()}": row["count"]
        for row in VillageDeliveryOutbox.objects.values("message_kind", "status")
        .annotate(count=Count("delivery_id"))
    }
    oldest = VillageDeliveryOutbox.objects.exclude(status="ACKED").aggregate(oldest=Min("created_at"))["oldest"]
    attempts = VillageDeliveryOutbox.objects.aggregate(max_attempts=Max("attempt_count"))["max_attempts"] or 0
    outbox_rows = [{
        "delivery_id": str(row.delivery_id),
        "recipient": row.recipient,
        "message_kind": row.message_kind,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "event_sequence": row.event.event_sequence if row.event else None,
        "last_error_code": row.last_error_code,
    } for row in VillageDeliveryOutbox.objects.select_related(
        "event"
    ).order_by("-created_at")[:100]]
    cursors = [{
        "recipient": row.recipient,
        "last_event_sequence": row.last_event_sequence,
        "updated_at": row.updated_at.isoformat(),
    } for row in VillageDeliveryCursor.objects.order_by("recipient")[:100]]
    receipt_metrics = VillageActionReceipt.objects.aggregate(
        total=Count("request_id"),
        failures=Count("request_id", filter=Q(success=False)),
    )
    receipt_total = receipt_metrics["total"] or 0
    command_status_counts = {
        row["status"].lower(): row["count"]
        for row in NpcCommandState.objects.values("status").annotate(count=Count("command_id"))
    }
    outbox = {
        "counts": counts,
        "oldest_unacked_age_seconds": max(0, int((now - oldest).total_seconds())) if oldest else 0,
        "max_attempt_count": attempts,
        "deliveries": outbox_rows,
        "cursors": cursors,
    }
    technical = {
        "world_time": {
            "world_minute": time_state.world_minute if time_state else None,
            "time_revision": time_state.time_revision if time_state else None,
        },
        "commands": commands,
        "command_status_counts": command_status_counts,
        "queue_length": sum(
            command_status_counts.get(status, 0) for status in ("received", "queued")
        ),
        "locks": locks,
        "outbox": outbox,
        "checkpoint": {
            "last_success_at": "unavailable",
            "latency_ms": "unavailable",
            "runtime_state_last_updated_at": max(
                (row.updated_at for row in runtimes), default=None
            ).isoformat() if runtimes else None,
        },
        "backend_settlement_failure_rate": (
            round(receipt_metrics["failures"] / receipt_total, 6) if receipt_total else 0
        ),
        "tick_latency_ms": {"p50": "unavailable", "p95": "unavailable", "p99": "unavailable"},
        "hub_failure_rate": "unavailable",
    }
    return {
        "npc": {"states": npcs},
        "economy": {
            "inventories": inventories,
            "accounts": accounts,
            "ledger_entries": ledger_entries,
            "orders": orders,
            "deliveries": deliveries,
        },
        "social": {
            "relationships": relationships,
            "commitments": commitments,
            "fact_access": fact_access,
        },
        "story": {
            "arcs": arcs,
            "facts": story_facts,
            "transition_receipts": transitions,
        },
        "technical": technical,
        # Keep the Phase 7 debug panel's existing read paths while it migrates
        # to the grouped operator DTO above.
        "npcs": npcs,
        "inventories": inventories,
        "commands": commands,
        "locks": locks,
        "outbox": outbox,
    }
