"""Minimal authoritative reducer for BROKEN_TRADE_ROUTE_V1."""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any, Mapping

from django.db import transaction

from game_time.models import GlobalGameTimeState
from inventory.models import (
    InventoryBag,
    InventorySlot,
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from items.models import Item

from .models import (
    EconomicLedgerEntry,
    NpcRuntimeState,
    NpcStoryFactAccess,
    PlayerStoryFactAccess,
    VillageAccount,
    VillageActionReceipt,
    VillageRouteAttempt,
    VillageSimulationConfig,
    VillageStoryArc,
    VillageStoryFact,
    VillageStoryMeeting,
    VillageStoryTransitionReceipt,
    VillageStoryVote,
    VillageWorldEvent,
)
from .phase4_social import grant_npc_fact, reduce_relationship
from .phase4_visibility import enqueue_event_deliveries
from .phase5_contract import (
    ALL_STORY_ACTIONS,
    ARC_CODE,
    FORMAL_NPC_IDS,
    PLAN_REQUIRED_FACT,
    SAFE_PLAN_PRIORITY,
    STORY_ACTION_ACTORS,
    STORY_ACTION_LOCATIONS,
    STORY_ACTION_PLANS,
    STORY_COMMAND_PARAM_WHITELIST,
    STORY_LOCATIONS,
    StoryActionCode,
    StoryCommand,
    StoryOutcome,
    StoryPhase,
    StoryPlan,
    StoryResultCode,
)


def _code(value):
    return getattr(value, "value", value)


def _hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _config():
    return VillageSimulationConfig.objects.filter(config_key="default").first()


def story_enabled() -> bool:
    return bool(_config() and _config().village_story_enabled)


def _result(code=StoryResultCode.OK, **data):
    succeeded = _code(code) == "OK"
    result = {
        "schema_version": "village.story.v1",
        "snapshot_schema_version": "village.story-snapshot.v1",
        "status": "SUCCEEDED" if succeeded else "FAILED",
        "success": succeeded,
        "result_code": _code(code),
        "replayed": False,
        "data": {},
        "event": {},
    }
    result.update(data)
    return result


def _event(*, event_type, request_id, world_minute, time_revision, actor=None,
           participants=None, players=None, location="RECORD_HALL", data=None,
           visibility="PUBLIC", suffix="event"):
    event = VillageWorldEvent.objects.create(
        dedupe_key=f"phase5:{request_id}:{suffix}"[:128],
        event_type=event_type,
        world_minute=world_minute,
        time_revision=time_revision,
        actor_npc=actor,
        participant_npc_ids=[str(x) for x in (participants or [])],
        player_ids=[str(x) for x in (players or [])],
        location_code=location or "",
        request_id=request_id,
        effects={},
        visibility=visibility,
        public_data=dict(data or {}),
        hub_summary=str((data or {}).get("hub_summary", event_type))[:256],
        player_summary=str((data or {}).get("player_summary", event_type))[:256],
    )
    enqueue_event_deliveries(event)
    return event


def _snapshot(arc, fact, meeting=None):
    facts = {
        "bridge_condition": fact.bridge_condition,
        "tool_stock_coverage_days": float(fact.tool_stock_coverage_days),
        "food_stock_coverage_days": float(fact.food_stock_coverage_days),
        "repair_fund": fact.repair_fund,
        "repair_wood": fact.repair_wood,
        "repair_metal": fact.repair_metal,
        "repair_labor_hours": fact.repair_labor_hours,
        "public_trust": fact.public_trust,
        "forest_route_known": fact.forest_route_known,
        "forest_route_scouted": fact.forest_route_scouted,
        "forest_route_supply": fact.forest_route_supply,
        "emergency_caravan_contacted": fact.emergency_caravan_contacted,
        "emergency_caravan_succeeded": fact.emergency_caravan_succeeded,
        "ration_rules_published": fact.ration_rules_published,
        "store_limits_enabled": fact.store_limits_enabled,
        "ration_daily_limits": fact.ration_daily_limits,
        "community_meals": fact.community_meals,
        "rationing_enabled": bool(
            arc.current_plan == StoryPlan.RATION_AND_WAIT.value
            and arc.current_phase == StoryPhase.EXECUTION.value
            and not arc.is_completed
        ),
        "evacuation_active": arc.outcome == StoryOutcome.PARTIAL_EVACUATION.value,
        "plan_progress": {
            "repair_fund": fact.repair_fund,
            "repair_wood": fact.repair_wood,
            "repair_metal": fact.repair_metal,
            "repair_labor_hours": fact.repair_labor_hours,
            "forest_route_scouted": fact.forest_route_scouted,
            "forest_route_supply": fact.forest_route_supply,
            "community_meals": fact.community_meals,
            "ration_daily_limits": fact.ration_daily_limits,
        },
    }
    meeting_data = None if meeting is None else {
        "meeting_id": str(meeting.meeting_id),
        "meeting_type": meeting.meeting_type,
        "opened_day": meeting.opened_day,
        "closes_day": meeting.closes_day,
        "candidate_plans": list(meeting.candidate_plans or []),
        "retry_count": meeting.retry_count,
        "status": meeting.status,
        "selected_plan": meeting.selected_plan or None,
        "attendance_count": meeting.attendance_count,
    }
    available_actions = [
        action for action, plans in STORY_ACTION_PLANS.items()
        if arc.current_plan and arc.current_plan in plans
    ]
    away_npc_ids = NpcRuntimeState.objects.filter(
        away_until_world_minute__isnull=False,
    ).values_list("npc__agent_id", flat=True)
    return {
        "schema_version": "village.story-snapshot.v1",
        "arc_code": arc.arc_code,
        "phase": arc.current_phase,
        "plan": arc.current_plan or None,
        "outcome": arc.outcome or None,
        "current_phase": arc.current_phase,
        "current_plan": arc.current_plan or None,
        "start_day": arc.start_day,
        "phase_entered_day": arc.phase_entered_day,
        "story_version": arc.story_version,
        "is_completed": arc.is_completed,
        "current_day": fact.last_progress_day,
        "bridge_condition": fact.bridge_condition,
        "meeting_open": meeting is not None,
        "meeting_id": str(meeting.meeting_id) if meeting else None,
        "meeting_retry_count": meeting.retry_count if meeting else 0,
        "candidate_plans": list(meeting.candidate_plans or []) if meeting else [],
        "available_actions": available_actions,
        "away_npc_ids": [str(value) for value in away_npc_ids],
        "rationing_enabled": facts["rationing_enabled"],
        "forest_event": "SUCCESS" if fact.emergency_caravan_succeeded else (
            "FAILED" if fact.emergency_caravan_contacted else ""
        ),
        "evacuation_active": facts["evacuation_active"],
        "plan_progress": facts["plan_progress"],
        "meeting": meeting_data,
        "facts": facts,
    }


def _project_facts(fact, arc, day):
    account = VillageAccount.objects.select_for_update().filter(account_code="PUBLIC_FUND").first()
    if account:
        fact.repair_fund = max(0, account.balance)
    slots = NpcGlobalInventorySlot.objects.filter(bag__npc__agent_id__in=FORMAL_NPC_IDS, item_amount__gt=0)
    tools = slots.filter(item_id__in=(4001, 4002))
    tool_uses = sum((row.remaining_uses if row.remaining_uses is not None else row.item_amount) for row in tools)
    food_ids = set(Item.objects.filter(item_type__in=("食物", "Food", "food")).values_list("item_id", flat=True))
    food_ids.add(29001)
    food_amount = sum(row.item_amount for row in slots.filter(item_id__in=food_ids))
    fact.tool_stock_coverage_days = Decimal(str(round(tool_uses / 2, 2)))
    fact.food_stock_coverage_days = Decimal(str(round(food_amount / 5, 2)))
    fact.forest_route_known = fact.forest_route_known or NpcStoryFactAccess.objects.filter(
        fact_code="FOREST_OLD_ROUTE", is_public=True
    ).exists()
    if fact.bridge_condition != "REPAIRED":
        if fact.repair_wood >= (_config().story_repair_wood_target if _config() else 10) and fact.repair_metal >= (_config().story_repair_metal_target if _config() else 4):
            fact.bridge_condition = "MATERIALS_READY"
    fact.save()
    return fact


def _candidate_plans(fact):
    plans = [StoryPlan.BRIDGE_REPAIR.value, StoryPlan.RATION_AND_WAIT.value]
    if fact.forest_route_known:
        plans.insert(1, StoryPlan.FOREST_ROUTE.value)
    return plans


def _get_arc_locked(world_minute, request_id=None):
    day = max(0, int(world_minute) // 1440)
    arc = VillageStoryArc.objects.select_for_update().filter(arc_code=ARC_CODE).first()
    if arc:
        return arc, VillageStoryFact.objects.select_for_update().get(arc=arc)
    arc = VillageStoryArc.objects.create(
        arc_code=ARC_CODE, current_phase=StoryPhase.AFTERMATH.value,
        start_day=day, phase_entered_day=day,
    )
    fact = VillageStoryFact.objects.create(arc=arc, last_progress_day=day)
    rid = request_id or uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:bootstrap:{day}")
    time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
    _event(event_type="STORY_BOOTSTRAPPED", request_id=rid, world_minute=world_minute,
           time_revision=time.time_revision, data={"arc_code": ARC_CODE}, suffix="bootstrap")
    return arc, fact


@transaction.atomic
def bootstrap_story(request_id=None):
    if not story_enabled():
        return _result(StoryResultCode.FEATURE_DISABLED)
    time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
    arc, fact = _get_arc_locked(time.world_minute, _uuid(request_id))
    fact = _project_facts(fact, arc, time.world_minute // 1440)
    meeting = arc.meetings.filter(status="OPEN").order_by("opened_day").first()
    return _result(data=_snapshot(arc, fact, meeting))


@transaction.atomic
def get_story_snapshot():
    if not story_enabled():
        return _result(StoryResultCode.FEATURE_DISABLED)
    arc = VillageStoryArc.objects.filter(arc_code=ARC_CODE).first()
    if not arc:
        return bootstrap_story()
    fact = VillageStoryFact.objects.get(arc=arc)
    time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
    fact = _project_facts(fact, arc, time.world_minute // 1440)
    meeting = arc.meetings.filter(status="OPEN").order_by("opened_day").first()
    return _result(data=_snapshot(arc, fact, meeting))


def _existing_request(request_id, payload_hash):
    receipt = VillageActionReceipt.objects.filter(request_id=request_id).first()
    if not receipt:
        return None
    stored = (receipt.response_data or {}).get("request_payload_sha256")
    if stored != payload_hash:
        return _result(StoryResultCode.REQUEST_CONFLICT, status="CONFLICT")
    result = dict(receipt.response_data or {})
    result.pop("request_payload_sha256", None)
    result["replayed"] = True
    return result


def _record(request_id, payload_hash, action, result, *, actor=None, players=None,
            world_minute, time_revision, event_type, data=None, ledger=None):
    event = _event(event_type=event_type, request_id=request_id, world_minute=world_minute,
                   time_revision=time_revision, actor=actor, participants=[actor.agent_id] if actor else [],
                   players=players, data=data or result.get("data", {}), suffix=action)
    result["event"] = {"event_id": str(event.event_id), "event_sequence": event.event_sequence}
    stored = dict(result)
    stored["request_payload_sha256"] = payload_hash
    VillageActionReceipt.objects.create(
        request_id=request_id, action_kind=action, success=result["status"] == "SUCCEEDED",
        result_code=result["result_code"], response_data=stored, world_minute=world_minute,
        time_revision=time_revision, npc=actor, world_event=event,
    )
    if ledger is not None:
        EconomicLedgerEntry.objects.create(
            request_id=request_id, entry_type=f"STORY_{action}", actor_npc=actor,
            changes=ledger.get("changes", {}), external_delta=ledger.get("external_delta", 0),
            world_minute=world_minute, event=event,
        )
    if actor is not None:
        for other_id in FORMAL_NPC_IDS - {str(actor.agent_id)}:
            reduce_relationship(
                event, str(actor.agent_id), other_id, "JOINT_WORK",
                {"trust": 2, "affinity": 1}, major=False,
            )
    return result


def _attendees():
    rows = NpcRuntimeState.objects.select_for_update().select_related("npc").filter(
        npc__agent_id__in=FORMAL_NPC_IDS, current_location_code="RECORD_HALL",
    ).exclude(action_status="SLEEPING")
    return {str(row.npc.agent_id): row.npc for row in rows if not row.away_until_world_minute}


@transaction.atomic
def submit_story_vote(payload: Mapping[str, Any], *, player_id=None):
    if not story_enabled():
        return _result(StoryResultCode.FEATURE_DISABLED)
    request_id = _uuid(payload.get("request_id"))
    if not request_id:
        return _result(StoryResultCode.ACTION_INVALID)
    expected = payload.get("expected_story_version", payload.get("params", {}).get("expected_story_version"))
    if isinstance(expected, bool) or not isinstance(expected, int):
        return _result(StoryResultCode.VERSION_CONFLICT)
    params = dict(payload.get("params") or payload)
    allowed = STORY_COMMAND_PARAM_WHITELIST[StoryCommand.VOTE.value]
    if set(params) - allowed or not {"meeting_id", "plan_code", "expected_story_version"}.issubset(params):
        return _result(StoryResultCode.VOTE_INVALID)
    plan = str(params.get("plan_code", "")).upper()
    meeting_id = _uuid(params.get("meeting_id"))
    if not meeting_id or plan not in {p.value for p in StoryPlan}:
        return _result(StoryResultCode.VOTE_INVALID)
    digest = _hash(payload)
    with transaction.atomic():
        time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        arc, fact = _get_arc_locked(time.world_minute, request_id)
        replay = _existing_request(request_id, digest)
        if replay:
            return replay
        if arc.is_completed:
            return _result(StoryResultCode.ARC_COMPLETED)
        if arc.story_version != expected:
            return _result(StoryResultCode.VERSION_CONFLICT, current_story_version=arc.story_version)
        meeting = VillageStoryMeeting.objects.select_for_update().filter(meeting_id=meeting_id, arc=arc).first()
        if not meeting or meeting.status != "OPEN":
            return _result(StoryResultCode.MEETING_ALREADY_SETTLED)
        if time.world_minute // 1440 > meeting.closes_day:
            return _result(StoryResultCode.MEETING_NOT_OPEN)
        if plan not in meeting.candidate_plans:
            return _result(StoryResultCode.FACT_NOT_UNLOCKED if plan == StoryPlan.FOREST_ROUTE.value else StoryResultCode.VOTE_INVALID)
        voter_type = "PLAYER" if player_id is not None or str(payload.get("actor_type", "")).upper() == "PLAYER" else "NPC"
        voter_id = str(player_id if player_id is not None else payload.get("npc_id", ""))
        if voter_type == "NPC" and voter_id not in FORMAL_NPC_IDS:
            return _result(StoryResultCode.FORBIDDEN_ACTOR)
        if voter_type == "PLAYER" and not voter_id:
            return _result(StoryResultCode.PLAYER_AUTH_REQUIRED)
        if voter_type == "NPC" and voter_id not in _attendees():
            return _result(StoryResultCode.NOT_ENOUGH_ATTENDANCE)
        previous = VillageStoryVote.objects.filter(meeting=meeting, voter_type=voter_type, voter_id=voter_id).first()
        if previous:
            previous.plan_code = plan
            previous.request_id = request_id
            previous.payload_hash = digest
            previous.submitted_world_minute = time.world_minute
            previous.player_support = voter_type == "PLAYER"
            previous.save()
        else:
            VillageStoryVote.objects.create(
                meeting=meeting, voter_type=voter_type, voter_id=voter_id, plan_code=plan,
                player_support=voter_type == "PLAYER", request_id=request_id,
                payload_hash=digest, submitted_world_minute=time.world_minute,
            )
        result = _result(data={"meeting_id": str(meeting_id), "plan_code": plan, "story_version": arc.story_version})
        return _record(request_id, digest, "STORY_VOTE", result,
                       actor=NpcDefinition.objects.filter(agent_id=voter_id).first() if voter_type == "NPC" else None,
                       players=[voter_id] if voter_type == "PLAYER" else [], world_minute=time.world_minute,
                       time_revision=time.time_revision, event_type="STORY_VOTE_CAST")


def _pick_plan(meeting, attendees):
    votes = {row.voter_id: row for row in meeting.votes.filter(voter_type="NPC", is_valid=True)}
    counts = {plan: 0 for plan in meeting.candidate_plans}
    for npc_id in attendees:
        row = votes.get(npc_id)
        if row and row.plan_code in counts:
            counts[row.plan_code] += 1
    maximum = max(counts.values(), default=0)
    if maximum == 0:
        return None
    winners = [plan for plan, count in counts.items() if count == maximum]
    if len(winners) == 1:
        return winners[0]
    support = {plan: 0 for plan in winners}
    for row in meeting.votes.filter(voter_type="PLAYER", player_support=True, plan_code__in=winners):
        support[row.plan_code] += 1
    if support:
        supported = max(support.values())
        if supported:
            winners = [plan for plan in winners if support[plan] == supported]
    return next((plan for plan in SAFE_PLAN_PRIORITY if plan in winners), sorted(winners)[0])


@transaction.atomic
def settle_story_meeting(meeting_id, expected_story_version=None, request_id=None, world_minute=None):
    if not story_enabled():
        return _result(StoryResultCode.FEATURE_DISABLED)
    time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
    world_minute = time.world_minute if world_minute is None else world_minute
    rid = _uuid(request_id) or uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:meeting:{meeting_id}:{world_minute // 1440}")
    arc = VillageStoryArc.objects.select_for_update().get(arc_code=ARC_CODE)
    fact = VillageStoryFact.objects.select_for_update().get(arc=arc)
    meeting = VillageStoryMeeting.objects.select_for_update().get(meeting_id=meeting_id, arc=arc)
    if meeting.status != "OPEN":
        return _result(StoryResultCode.MEETING_ALREADY_SETTLED, data=_snapshot(arc, fact, meeting))
    if expected_story_version is not None and arc.story_version != expected_story_version:
        return _result(StoryResultCode.VERSION_CONFLICT, current_story_version=arc.story_version)
    attendees = _attendees()
    meeting.attendance_count = len(attendees)
    plan = _pick_plan(meeting, attendees)
    valid_votes = sum(1 for row in meeting.votes.filter(voter_type="NPC", is_valid=True) if row.voter_id in attendees and row.plan_code in meeting.candidate_plans)
    if len(attendees) < 3 or valid_votes < 3 or plan is None:
        meeting.failure_code = StoryResultCode.NOT_ENOUGH_ATTENDANCE.value
        if meeting.retry_count == 0:
            meeting.retry_count = 1
            meeting.closes_day += 1
            meeting.save()
            return _result(StoryResultCode.NOT_ENOUGH_ATTENDANCE, data={"retry_count": 1, "retry_day": meeting.closes_day})
        meeting.status = "SETTLED"
        meeting.selected_plan = StoryPlan.RATION_AND_WAIT.value
        meeting.settled_day = world_minute // 1440
        meeting.save()
        plan = StoryPlan.RATION_AND_WAIT.value
        retry = True
    else:
        meeting.status = "SETTLED"
        meeting.selected_plan = plan
        meeting.settled_day = world_minute // 1440
        meeting.save()
        retry = False
    from_phase = arc.current_phase
    arc.current_phase = StoryPhase.EXECUTION.value
    arc.current_plan = plan
    arc.phase_entered_day = world_minute // 1440
    fact.last_progress_day = world_minute // 1440
    fact.save(update_fields=["last_progress_day", "updated_at"])
    if plan == StoryPlan.RATION_AND_WAIT.value:
        arc.ration_until_day = world_minute // 1440 + (2 if retry else 3)
    else:
        arc.ration_until_day = None
    arc.story_version += 1
    arc.save()
    event = _event(event_type="STORY_MEETING_SETTLED", request_id=rid, world_minute=world_minute,
                   time_revision=time.time_revision, participants=list(attendees),
                   data={"plan_code": plan, "retry_fallback": retry}, suffix="meeting")
    VillageStoryTransitionReceipt.objects.create(
        request_id=rid, arc=arc, from_phase=from_phase, to_phase=StoryPhase.EXECUTION.value,
        selected_plan=plan, source_day=world_minute // 1440, world_event=event,
        response_data={"retry_fallback": retry},
    )
    return _result(data={"plan_code": plan, "retry_fallback": retry, "story_version": arc.story_version,
                         "snapshot": _snapshot(arc, fact, meeting)}, event={"event_id": str(event.event_id)})


def _consume(npc_id, item_id, amount, player_id=None):
    if amount <= 0:
        return True
    if player_id:
        bag = InventoryBag.objects.select_for_update().filter(character_id=str(player_id)).first()
        if not bag:
            return False
        slots = list(InventorySlot.objects.select_for_update().filter(bag=bag, item_id=item_id, item_amount__gt=0).order_by("slot_index"))
    else:
        bag = NpcGlobalInventory.objects.select_for_update().filter(npc__agent_id=str(npc_id)).first()
        if not bag:
            return False
        slots = list(NpcGlobalInventorySlot.objects.select_for_update().filter(bag=bag, item_id=item_id, item_amount__gt=0).order_by("slot_index"))
    if sum(row.item_amount for row in slots) < amount:
        return False
    remaining = amount
    for row in slots:
        take = min(remaining, row.item_amount)
        row.item_amount -= take
        row.save(update_fields=["item_amount"])
        remaining -= take
        if not remaining:
            break
    if not player_id:
        bag.asset_version += 1
        bag.save(update_fields=["asset_version", "updated_at"])
    return True


def _int_value(value, minimum=0):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= minimum else None


def _money(npc_id, amount, player_id=None):
    if amount <= 0:
        return None
    if player_id:
        bag = InventoryBag.objects.select_for_update().filter(character_id=str(player_id)).first()
    else:
        bag = NpcGlobalInventory.objects.select_for_update().filter(npc__agent_id=str(npc_id)).first()
    if not bag or bag.money < amount:
        return None
    bag.money -= amount
    if player_id:
        bag.save(update_fields=["money"])
    else:
        bag.asset_version += 1
        bag.save(update_fields=["money", "asset_version"])
    return bag


@transaction.atomic
def complete_story_action(payload: Mapping[str, Any], *, player_id=None):
    if not story_enabled():
        return _result(StoryResultCode.FEATURE_DISABLED)
    request_id = _uuid(payload.get("request_id"))
    if not request_id:
        return _result(StoryResultCode.ACTION_INVALID)
    digest = _hash(payload)
    params = dict(payload.get("params") or payload)
    action = str(payload.get("action_code", params.get("action_code", ""))).upper()
    expected = payload.get("expected_story_version", params.get("expected_story_version"))
    if action not in ALL_STORY_ACTIONS or isinstance(expected, bool) or not isinstance(expected, int):
        return _result(StoryResultCode.ACTION_INVALID)
    if (
        set(params) - STORY_COMMAND_PARAM_WHITELIST[StoryCommand.ACT.value]
        or "action_code" not in params
        or "expected_story_version" not in params
    ):
        return _result(StoryResultCode.ACTION_INVALID)
    with transaction.atomic():
        time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        arc, fact = _get_arc_locked(time.world_minute, request_id)
        replay = _existing_request(request_id, digest)
        if replay:
            return replay
        if arc.is_completed:
            return _result(StoryResultCode.ARC_COMPLETED)
        if arc.story_version != expected:
            return _result(StoryResultCode.VERSION_CONFLICT, current_story_version=arc.story_version)
        plan = arc.current_plan
        if plan not in STORY_ACTION_PLANS.get(action, set()):
            return _result(StoryResultCode.PLAN_INVALID)
        actor_id = str(payload.get("npc_id", params.get("npc_id", "")))
        actor = NpcDefinition.objects.filter(agent_id=actor_id, is_test_only=False).first() if actor_id else None
        player_actions = {
            StoryActionCode.DONATE_REPAIR_RESOURCES.value,
            StoryActionCode.SUPPLY_FOREST_ROUTE.value,
            StoryActionCode.PROVIDE_REPAIR_MEAL.value,
            StoryActionCode.PROVIDE_COMMUNITY_MEAL.value,
        }
        if player_id is not None and action not in player_actions:
            return _result(StoryResultCode.FORBIDDEN_ACTOR)
        if player_id is None and actor_id not in STORY_ACTION_ACTORS.get(action, set()):
            return _result(StoryResultCode.FORBIDDEN_ACTOR)
        location = str(payload.get("current_location_code", params.get("location_code", "")))
        if player_id is None and location not in STORY_ACTION_LOCATIONS.get(action, set()):
            return _result(StoryResultCode.LOCATION_INVALID)
        cost = {"gold": 0}
        changes = {"action_code": action}
        if action == StoryActionCode.SCOUT_FOREST_ROUTE.value:
            fact.forest_route_scouted += 1
            fact.forest_route_known = True
            access = grant_npc_fact(actor_id, "FOREST_OLD_ROUTE", time.world_minute)
            if access and not access.is_public:
                access.is_public = True
                access.save(update_fields=["is_public"])
        elif action == StoryActionCode.PUBLISH_RATION_RULES.value:
            fact.ration_rules_published = True
        elif action == StoryActionCode.ENABLE_STORE_LIMITS.value:
            item_id = _int_value(params.get("limit_item_id"), 1)
            daily_limit = _int_value(params.get("daily_limit"), 1)
            if item_id is None or daily_limit is None:
                return _result(StoryResultCode.ACTION_INVALID)
            fact.store_limits_enabled = True
            limits = dict(fact.ration_daily_limits or {})
            limits[str(item_id)] = daily_limit
            fact.ration_daily_limits = limits
            changes["ration_limit"] = {"item_id": item_id, "daily_limit": daily_limit}
        elif action == StoryActionCode.REPAIR_LABOR.value:
            hours = _int_value(params.get("labor_hours", 1), 1)
            if hours is None:
                return _result(StoryResultCode.ACTION_INVALID)
            fact.repair_labor_hours += hours
            fact.bridge_condition = "REPAIRING"
            fact.public_trust = min(100, fact.public_trust + 1)
            changes["labor_hours"] = hours
        elif action == StoryActionCode.PROVIDE_REPAIR_MEAL.value or action == StoryActionCode.PROVIDE_COMMUNITY_MEAL.value:
            item_id = _int_value(params.get("meal_item_id", 29001), 1)
            amount = _int_value(params.get("meal_amount", 1), 1)
            if item_id is None or amount is None:
                return _result(StoryResultCode.ACTION_INVALID)
            if player_id is None and not _consume(actor_id, item_id, amount):
                return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
            if player_id is not None and not _consume("", item_id, amount, player_id):
                return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
            fact.community_meals += amount
        elif action == StoryActionCode.DONATE_REPAIR_RESOURCES.value:
            item_id = params.get("item_id")
            item_id = None if item_id is None else _int_value(item_id, 1)
            amount = _int_value(params.get("amount", 0), 0)
            gold = _int_value(params.get("gold_amount", 0), 0)
            if amount is None or gold is None or (params.get("item_id") is not None and item_id is None):
                return _result(StoryResultCode.ACTION_INVALID)
            if item_id is None and gold <= 0:
                return _result(StoryResultCode.ACTION_INVALID)
            if item_id is not None and item_id not in (1003, 4003):
                return _result(StoryResultCode.ACTION_INVALID)
            if item_id is not None and (amount <= 0 or not _consume(actor_id, item_id, amount, player_id)):
                return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
            if item_id is not None:
                if item_id == 1003:
                    fact.repair_wood += amount
                else:
                    fact.repair_metal += amount
                changes["item"] = {"item_id": item_id, "amount": amount}
            if gold:
                if not _money(actor_id, gold, player_id):
                    return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
                fund = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
                fund.balance += gold
                fund.asset_version += 1
                fund.save(update_fields=["balance", "asset_version"])
                fact.repair_fund += gold
                changes["gold"] = gold
            fact.public_trust = min(100, fact.public_trust + 2)
        elif action == StoryActionCode.PURCHASE_REPAIR_MATERIALS.value:
            gold = _int_value(params.get("gold_amount", 50), 1)
            amount = _int_value(params.get("amount", 1), 1)
            item_id = _int_value(params.get("item_id", 4003), 1)
            if gold is None or amount is None or item_id != 4003:
                return _result(StoryResultCode.ACTION_INVALID)
            fund = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
            if fund.balance < gold:
                return _result(StoryResultCode.FUND_INSUFFICIENT)
            fund.balance -= gold
            fund.asset_version += 1
            fund.save(update_fields=["balance", "asset_version"])
            fact.repair_metal += amount
            changes.update({"gold": -gold, "item": {"item_id": 4003, "amount": amount}})
        elif action == StoryActionCode.CONTACT_EMERGENCY_CARAVAN.value:
            attempt_id = _uuid(params.get("route_attempt_id"))
            if not attempt_id:
                return _result(StoryResultCode.ACTION_INVALID)
            old = VillageRouteAttempt.objects.select_for_update().filter(route_attempt_id=attempt_id).first()
            if old:
                return _result(StoryResultCode.ROUTE_ATTEMPT_REPLAY, data={
                    "result_code": old.result_code,
                    "route_attempt_id": str(attempt_id),
                    "progress": old.progress,
                })
            if fact.forest_route_scouted < 2 or fact.forest_route_supply < 1:
                return _result(StoryResultCode.NOT_READY)
            frozen = {
                "scouted": fact.forest_route_scouted,
                "supply": fact.forest_route_supply,
                "companion_npc_id": str(params.get("companion_npc_id", "")),
                "segment_code": str(params.get("segment_code", "")),
                "health": NpcRuntimeState.objects.filter(npc__agent_id="10012").values_list("health_fp", flat=True).first() or 1000,
            }
            cfg = _config()
            seed = f"{cfg.story_fixed_seed if cfg else 'village-story-v1'}:{attempt_id}:{json.dumps(frozen, sort_keys=True)}"
            roll = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % 100
            success = roll < 65
            gold = _int_value(params.get("gold_amount", 100), 1)
            if gold is None:
                return _result(StoryResultCode.ACTION_INVALID)
            cost = {"gold": gold, "supply": 1}
            if not _money(actor_id, cost["gold"]):
                return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
            fact.forest_route_supply -= 1
            fact.emergency_caravan_contacted = True
            fact.emergency_caravan_succeeded = success
            attempt_result = "SUCCESS" if success else "FAILED"
            changes.update({"route_attempt_id": str(attempt_id), "route_result": attempt_result})
            injury = {} if success else {"npc_id": "10012", "health_delta": -50}
            if not success:
                NpcRuntimeState.objects.filter(npc__agent_id="10012").update(health_fp=0 if frozen["health"] < 50 else frozen["health"] - 50)
            VillageRouteAttempt.objects.create(
                route_attempt_id=attempt_id, arc=arc, request_id=request_id,
                frozen_input=frozen, seed=seed, result_code=attempt_result,
                cost=cost, injury=injury, progress={"supply_spent": 1, "succeeded": success},
                created_world_minute=time.world_minute,
            )
        elif action == StoryActionCode.SUPPLY_FOREST_ROUTE.value:
            item_id = _int_value(params.get("supply_item_id", params.get("item_id", 29001)), 1)
            amount = _int_value(params.get("supply_amount", params.get("amount", 1)), 1)
            if item_id is None or amount is None:
                return _result(StoryResultCode.ACTION_INVALID)
            if not _consume(actor_id, item_id, amount, player_id):
                return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
            fact.forest_route_supply += amount
        elif action == StoryActionCode.TRANSPORT_REPAIR_MATERIALS.value:
            if actor_id != "10012":
                return _result(StoryResultCode.FORBIDDEN_ACTOR)
            cfg = _config()
            if fact.repair_wood < (cfg.story_repair_wood_target if cfg else 10) or fact.repair_metal < (cfg.story_repair_metal_target if cfg else 4):
                return _result(StoryResultCode.NOT_READY)
            fact.bridge_condition = "MATERIALS_READY"
        elif action == StoryActionCode.MANAGE_REPAIR_LEDGER.value:
            fact.public_trust = min(100, fact.public_trust + 1)
        fact.last_progress_day = time.world_minute // 1440
        fact = _project_facts(fact, arc, time.world_minute // 1440)
        arc.story_version += 1
        arc.save(update_fields=["story_version", "updated_at"])
        cfg = _config()
        if fact.repair_fund >= (cfg.story_repair_fund_target if cfg else 500) and fact.repair_wood >= (cfg.story_repair_wood_target if cfg else 10) and fact.repair_metal >= (cfg.story_repair_metal_target if cfg else 4) and fact.repair_labor_hours >= (cfg.story_repair_labor_target if cfg else 24):
            fact.bridge_condition = "REPAIRED"
            fact.save(update_fields=["bridge_condition", "updated_at"])
        result = _result(data={"action_code": action, "story_version": arc.story_version, "snapshot": _snapshot(arc, fact)})
        ledger = {"changes": changes}
        if action == StoryActionCode.CONTACT_EMERGENCY_CARAVAN.value:
            ledger["external_delta"] = -cost["gold"]
        return _record(request_id, digest, "STORY_ACT", result, actor=actor,
                       players=[str(player_id)] if player_id else [], world_minute=time.world_minute,
                       time_revision=time.time_revision, event_type=f"STORY_{action}", ledger=ledger)


def _finish(arc, fact, outcome, day, time, request_id):
    from_phase = arc.current_phase
    arc.is_completed = True
    arc.outcome = outcome
    arc.current_phase = StoryPhase.CRISIS_CHECK.value
    arc.story_version += 1
    arc.save()
    if outcome == StoryOutcome.PARTIAL_EVACUATION.value:
        runtimes = list(NpcRuntimeState.objects.select_for_update().filter(
            npc__agent_id__in=FORMAL_NPC_IDS,
        ).order_by("health_fp", "-stress_fp", "npc__agent_id")[:1])
        for runtime in runtimes:
            runtime.away_until_world_minute = time.world_minute + 4 * 1440
            runtime.save(update_fields=["away_until_world_minute", "updated_at"])
    event = _event(event_type="STORY_OUTCOME", request_id=request_id, world_minute=time.world_minute,
                   time_revision=time.time_revision, data={"outcome": outcome}, suffix="outcome")
    if outcome == StoryOutcome.CONTROLLED_TRADE.value:
        for target_id in ("10011", "10012"):
            reduce_relationship(
                event, "10015", target_id, "BREACH",
                {"trust": -3, "conflict": 4}, major=True,
            )
    elif outcome == StoryOutcome.LONG_SHORTAGE.value:
        fact.ration_rules_published = True
        fact.store_limits_enabled = True
        fact.save(update_fields=["ration_rules_published", "store_limits_enabled", "updated_at"])
    VillageStoryTransitionReceipt.objects.create(
        request_id=request_id, arc=arc, from_phase=from_phase,
        to_phase=outcome, source_day=day, world_event=event, response_data={"outcome": outcome},
    )


@transaction.atomic
def process_day_close(day_index: int, world_minute: int, source_request_id=None):
    if not story_enabled():
        return _result(StoryResultCode.FEATURE_DISABLED)
    time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
    arc, fact = _get_arc_locked(world_minute, _uuid(source_request_id))
    if arc.is_completed:
        _return_away(time)
        return _result(data=_snapshot(arc, fact))
    day = int(day_index)
    fact = _project_facts(fact, arc, day)
    latest_day = arc.start_day + (_config().story_latest_day if _config() else 25)
    if arc.current_phase == StoryPhase.CRISIS_CHECK.value and day < latest_day:
        arc.last_crisis_day = day
        if fact.food_stock_coverage_days >= 1 or fact.public_trust >= (_config().story_public_trust_minimum if _config() else 20):
            _advance(
                arc, fact, StoryPhase.PLAN_REVIEW.value, day, time,
                uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{day}:crisis-review"),
            )
            arc.review_count += 1
            arc.save(update_fields=["review_count", "last_crisis_day", "updated_at"])
        else:
            arc.current_phase = StoryPhase.EXECUTION.value
            arc.ration_until_day = day + 2
            arc.story_version += 1
            arc.save(update_fields=["current_phase", "ration_until_day", "story_version", "last_crisis_day", "updated_at"])
    if arc.current_phase == StoryPhase.AFTERMATH.value and (day - arc.start_day >= 3 or fact.food_stock_coverage_days < 4 or fact.tool_stock_coverage_days < 7):
        _advance(arc, fact, StoryPhase.SHORTAGE_VISIBLE.value, day, time, uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{day}:shortage"))
    elif arc.current_phase == StoryPhase.SHORTAGE_VISIBLE.value and day >= arc.phase_entered_day + 1:
        # Jack's private route knowledge is explicitly shared when the first
        # council is called; private facts never unlock a candidate directly.
        NpcStoryFactAccess.objects.filter(
            fact_code="FOREST_OLD_ROUTE", is_public=False,
        ).update(is_public=True)
        fact.forest_route_known = True
        fact.save(update_fields=["forest_route_known", "updated_at"])
        _advance(arc, fact, StoryPhase.FIRST_COUNCIL.value, day, time, uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{day}:council"))
    if arc.current_phase in (StoryPhase.FIRST_COUNCIL.value, StoryPhase.PLAN_REVIEW.value):
        meeting = arc.meetings.filter(status="OPEN").order_by("opened_day").first()
        if not meeting:
            meeting = VillageStoryMeeting.objects.create(
                arc=arc, meeting_type=arc.current_phase, opened_day=day,
                closes_day=day, candidate_plans=_candidate_plans(fact),
            )
        if day >= meeting.closes_day:
            settle_story_meeting(meeting.meeting_id, expected_story_version=arc.story_version,
                                 request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{meeting.meeting_id}:{day}"), world_minute=world_minute)
            arc.refresh_from_db()
            fact.refresh_from_db()
    if arc.current_phase == StoryPhase.EXECUTION.value:
        if arc.current_plan == StoryPlan.BRIDGE_REPAIR.value and fact.bridge_condition == "REPAIRED" and fact.public_trust >= (_config().story_public_trust_minimum if _config() else 20):
            rid = uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{day}:recovered")
            _finish(arc, fact, StoryOutcome.RECOVERED.value, day, time, rid)
        elif (
            arc.current_plan == StoryPlan.FOREST_ROUTE.value
            and fact.emergency_caravan_succeeded
            and (
                fact.public_trust < (_config().story_public_trust_minimum if _config() else 20)
                or fact.repair_fund < (_config().story_repair_fund_target if _config() else 500)
            )
        ):
            _finish(arc, fact, StoryOutcome.CONTROLLED_TRADE.value, day, time, uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{day}:controlled"))
        elif arc.current_plan == StoryPlan.RATION_AND_WAIT.value and day >= (arc.ration_until_day or day + 3):
            arc.current_phase = StoryPhase.CRISIS_CHECK.value
            arc.save(update_fields=["current_phase", "updated_at"])
        elif day - fact.last_progress_day >= 2:
            _advance(
                arc, fact, StoryPhase.PLAN_REVIEW.value, day, time,
                uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{day}:review"),
            )
            arc.review_count += 1
            arc.last_crisis_day = day
            arc.save(update_fields=["review_count", "last_crisis_day", "updated_at"])
    if not arc.is_completed and day >= latest_day:
        arc.current_phase = StoryPhase.CRISIS_CHECK.value
        arc.save(update_fields=["current_phase", "updated_at"])
        outcome = StoryOutcome.PARTIAL_EVACUATION.value if fact.food_stock_coverage_days < 1 and fact.public_trust < 10 else StoryOutcome.LONG_SHORTAGE.value
        _finish(arc, fact, outcome, day, time, uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{day}:deadline"))
    return _result(data=_snapshot(arc, fact, arc.meetings.filter(status="OPEN").first()))


def get_store_purchase_limit(npc_id, item_id, world_minute):
    """Return remaining daily units, or None when the story does not limit buys."""
    if not story_enabled():
        return None
    arc = VillageStoryArc.objects.filter(
        arc_code=ARC_CODE, is_completed=False, current_plan=StoryPlan.RATION_AND_WAIT.value,
    ).first()
    if not arc:
        return None
    fact = VillageStoryFact.objects.filter(arc=arc, store_limits_enabled=True).first()
    limit = (fact.ration_daily_limits or {}).get(str(item_id)) if fact else None
    if limit is None:
        return None
    start = (int(world_minute) // 1440) * 1440
    used = 0
    for changes in EconomicLedgerEntry.objects.filter(
        entry_type="BUY_FROM_STORE", actor_npc__agent_id=str(npc_id),
        world_minute__gte=start, world_minute__lt=start + 1440,
    ).values_list("changes", flat=True):
        used += int((changes or {}).get(str(npc_id), {}).get(str(item_id), 0) or 0)
    return max(0, int(limit) - used)


def _advance(arc, fact, to_phase, day, time, request_id, plan=""):
    from_phase = arc.current_phase
    arc.current_phase = to_phase
    arc.phase_entered_day = day
    arc.story_version += 1
    arc.save(update_fields=["current_phase", "phase_entered_day", "story_version", "updated_at"])
    event = _event(event_type="STORY_TRANSITION", request_id=request_id, world_minute=time.world_minute,
                   time_revision=time.time_revision, data={"from_phase": from_phase, "to_phase": to_phase}, suffix=to_phase)
    VillageStoryTransitionReceipt.objects.create(
        request_id=request_id, arc=arc, from_phase=from_phase, to_phase=to_phase,
        selected_plan=plan, source_day=day, world_event=event,
        response_data={"from_phase": from_phase, "to_phase": to_phase},
    )


def _return_away(time):
    rows = NpcRuntimeState.objects.select_for_update().filter(away_until_world_minute__isnull=False, away_until_world_minute__lte=time.world_minute).select_related("npc")
    for row in rows:
        row.away_until_world_minute = None
        row.save(update_fields=["away_until_world_minute", "updated_at"])
        _event(event_type="NPC_RETURNED", request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:return:{row.npc.agent_id}:{time.world_minute // 1440}"), world_minute=time.world_minute, time_revision=time.time_revision, actor=row.npc, participants=[row.npc.agent_id], data={"npc_id": str(row.npc.agent_id)}, suffix=f"return:{row.npc.agent_id}")


def dispatch_story_command(payload: Mapping[str, Any], *, player_id=None):
    command = str(payload.get("command", payload.get("command_name", payload.get("name", "")))).upper()
    if command == StoryCommand.VOTE.value:
        return submit_story_vote(payload, player_id=player_id)
    if command == StoryCommand.ACT.value:
        return complete_story_action(payload, player_id=player_id)
    return _result(StoryResultCode.ACTION_INVALID)


# Stable aliases used by Env/Hub adapters.
commit_story_action = complete_story_action
settle_story_action = complete_story_action
