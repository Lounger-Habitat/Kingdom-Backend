"""Minimal authoritative reducer for BROKEN_TRADE_ROUTE_V1."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
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
from .public_governance import (ADMIN_ACTIONS, actor_error, authorized_decision, approve_decision,
                                handle_admin, review_project, decision_data, meeting_state_error)
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
    event_data = dict(data or {})
    if event_type.startswith("STORY_"):
        event_data.setdefault("story_id", ARC_CODE)
        event_data.setdefault(
            "story_version",
            VillageStoryArc.objects.filter(arc_code=ARC_CODE).values_list("story_version", flat=True).first(),
        )
        event_data.setdefault("change_type", event_type)
        event_data.setdefault("summary", event_type)
        if event_type in {"STORY_CALL_MEETING", "STORY_REGISTER_PROPOSAL", "STORY_VOTE_CAST",
                          "STORY_MEETING_SETTLED", "STORY_PUBLISH_DECISION", "STORY_PUBLISH_RATION_RULES"}:
            decision = event_data.get("decision") or {}
            event_data.setdefault("meeting_id", decision.get("meeting_id"))
            event_data.setdefault("location_code", location)
            if decision.get("decision_id"):
                event_data["decision_id"] = decision["decision_id"]
            meeting = VillageStoryMeeting.objects.filter(pk=_uuid(event_data.get("meeting_id"))).first()
            if meeting:
                event_data.update({"starts_world_minute": meeting.starts_world_minute,
                                   "closes_day": meeting.closes_day, "meeting_status": meeting.status})
            labels = {"BRIDGE_REPAIR": "修桥采购", "RATION_AND_WAIT": "配给与等待"}
            plan = event_data.get("plan_code") or decision.get("plan_code") or (event_data.get("proposal") or {}).get("plan_code")
            plan_label = labels.get(plan, plan or "公共议题")
            if event_type == "STORY_CALL_MEETING":
                start = event_data.get("starts_world_minute")
                try:
                    start_text = (datetime.min + timedelta(minutes=start)).strftime("%m月%d日%H:%M")
                except (TypeError, OverflowError):
                    start_text = "请查询会议详情"
                summary = f"Mark召集记录厅公共会议，开会时间{start_text}；会议编号{event_data.get('meeting_id')}。请查看提案，自行安排到场和投票。"
            elif event_type == "STORY_REGISTER_PROPOSAL":
                terms = event_data.get("proposal") or {}
                summary = f"会议{event_data.get('meeting_id')}已登记{plan_label}提案：预算{terms.get('budget_gold', 0)}金，执行人{terms.get('executor_npc_id', '')}；尚未表决生效。"
            elif event_type == "STORY_VOTE_CAST":
                summary = f"会议{event_data.get('meeting_id')}收到一张{plan_label}投票；请查询有效票数和结算条件，尚不能视为方案通过。"
            elif event_type == "STORY_MEETING_SETTLED":
                summary = f"会议{event_data.get('meeting_id')}已结算，选中{plan_label}；决定{decision.get('decision_id')}尚待正式公布，不能据此执行采购。"
            else:
                summary = f"会议{event_data.get('meeting_id')}的{plan_label}决定已正式公布；决定编号{decision.get('decision_id')}。执行前核对批准条款、预算及期限。"
            event_data["summary"] = summary
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
        public_data=event_data,
        hub_summary=str(event_data.get("hub_summary", event_data.get("summary", event_type)))[:256],
        player_summary=str(event_data.get("player_summary", event_data.get("summary", event_type)))[:256],
    )
    enqueue_event_deliveries(event)
    return event


# 二跑复盘（R3）：AFTERMATH 无开放行动时，每世界日至多一条"自主安排"通报。
# 措辞必须是"通报"而不是"指令"，否则会被 agent 当成新任务执行（回声教训）。
STORY_IDLE_NOTICE_TEXT = (
    "剧情通报：断掉的商路当前没有开放行动，等待日结评估自动推进"
    "（距起始日满 1 天、或粮食/工具告急时进入下一阶段）。这段时间由你自己安排："
    "符合职责的日常事项（照看农田、备货、烹饪、休息、与村民交谈）都可以做，什么都不做也可以。"
)


def _maybe_notify_story_idle(arc, day, world_minute, time, request_id):
    """剧情空窗兜底通报；幂等（dedupe_key 按世界日）。"""
    if arc.is_completed or arc.current_phase != StoryPhase.AFTERMATH.value:
        return None
    summary = STORY_IDLE_NOTICE_TEXT
    data = {
        "story_id": ARC_CODE,
        "story_version": arc.story_version,
        "change_type": "STORY_IDLE_NOTICE",
        "phase": arc.current_phase,
        "hub_summary": summary,
        "player_summary": summary,
    }
    event, created = VillageWorldEvent.objects.get_or_create(
        dedupe_key=f"story-idle-notice:{day}",
        defaults=dict(
            event_type="STORY_IDLE_NOTICE",
            world_minute=world_minute,
            time_revision=time.time_revision,
            participant_npc_ids=list(FORMAL_NPC_IDS),
            location_code="VILLAGE_SQUARE",
            request_id=request_id,
            effects={},
            visibility="PUBLIC",
            public_data=data,
            hub_summary=summary,
            player_summary=summary,
        ),
    )
    if created:
        enqueue_event_deliveries(event)
    return event


def _snapshot(arc, fact, meeting=None):
    minute = GlobalGameTimeState.objects.filter(pk=1).values_list("world_minute", flat=True).first() or 0
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
        "ration_limits_until_world_minute": fact.ration_limits_until_world_minute,
        "community_meals": fact.community_meals,
        "rationing_enabled": bool(
            fact.store_limits_enabled and
            (fact.ration_limits_until_world_minute is None or minute < fact.ration_limits_until_world_minute)
        ),
        "evacuation_active": arc.outcome == StoryOutcome.PARTIAL_EVACUATION.value,
        "plan_progress": {
            "repair_fund": fact.repair_fund,
            "repair_wood": fact.repair_wood,
            "repair_metal": fact.repair_metal,
            "repair_labor_hours": fact.repair_labor_hours,
            "community_meals": fact.community_meals,
            "ration_daily_limits": fact.ration_daily_limits,
            "ration_limits_until_world_minute": fact.ration_limits_until_world_minute,
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
        if arc.current_plan in plans
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
        "retired_plan": arc.current_plan == "FOREST_ROUTE",
        "historical_forest_progress": {
            "forest_route_known": fact.forest_route_known,
            "forest_route_scouted": fact.forest_route_scouted,
            "forest_route_supply": fact.forest_route_supply,
        },
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
    if fact.bridge_condition != "REPAIRED":
        if fact.repair_wood >= (_config().story_repair_wood_target if _config() else 10) and fact.repair_metal >= (_config().story_repair_metal_target if _config() else 4):
            fact.bridge_condition = "MATERIALS_READY"
    fact.save()
    return fact


def _candidate_plans(fact):
    plans = [StoryPlan.BRIDGE_REPAIR.value, StoryPlan.RATION_AND_WAIT.value]
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
           time_revision=time.time_revision, data={
               "story_id": ARC_CODE, "arc_code": ARC_CODE, "story_version": arc.story_version,
               "change_type": "INITIALIZED", "summary": "断掉的商路主线已开始，旧桥处于损坏状态",
               "snapshot": _snapshot(arc, fact),
           }, suffix="bootstrap")
    _maybe_notify_story_idle(arc, day, world_minute, time, rid)
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
    from .agent_feedback import add_item_names
    add_item_names(result)
    event_data = add_item_names(dict(data or result.get("data", {})))
    if event_type.startswith("STORY_"):
        event_data.setdefault("story_id", ARC_CODE)
        event_data.setdefault("story_version", result.get("data", {}).get("story_version"))
        event_data.setdefault("change_type", event_type)
        event_data.setdefault("summary", event_type)
    event = _event(event_type=event_type, request_id=request_id, world_minute=world_minute,
                   time_revision=time_revision, actor=actor, participants=[actor.agent_id] if actor else [],
                   players=players, data=event_data, suffix=action)
    result["event"] = {"event_id": str(event.event_id), "event_sequence": event.event_sequence}
    stored = dict(result)
    stored["request_payload_sha256"] = payload_hash
    receipt = VillageActionReceipt.objects.create(
        request_id=request_id, action_kind=action, success=result["status"] == "SUCCEEDED",
        result_code=result["result_code"], response_data=stored, world_minute=world_minute,
        time_revision=time_revision, npc=actor, world_event=event,
    )
    from .runtime_services import OutboxService
    OutboxService.ensure_outcome(receipt)
    if ledger is not None:
        EconomicLedgerEntry.objects.create(
            request_id=request_id, entry_type=f"STORY_{action}", actor_npc=actor,
            changes=ledger.get("changes", {}), external_delta=ledger.get("external_delta", 0),
            world_minute=world_minute, event=event,
        )
    if actor is not None and event_type in {
        "STORY_DONATE_REPAIR_RESOURCES", "STORY_REPAIR_LABOR", "STORY_PROVIDE_REPAIR_MEAL",
        "STORY_TRANSPORT_REPAIR_MATERIALS",
    }:
        for other_id in FORMAL_NPC_IDS - {str(actor.agent_id)}:
            reduce_relationship(
                event, str(actor.agent_id), other_id, "JOINT_WORK",
                {"trust": 2, "affinity": 1}, major=False,
            )
    return result


def _attendees(world_minute=None, *, lock=True):
    rows = NpcRuntimeState.objects.select_related("npc").filter(
        npc__agent_id__in=FORMAL_NPC_IDS, current_location_code="RECORD_HALL",
    ).exclude(action_status__in=["SLEEPING", "AWAY"])
    if lock:
        rows = rows.select_for_update()
    minute = GlobalGameTimeState.objects.get(pk=1).world_minute if world_minute is None else world_minute
    return {str(row.npc.agent_id): row.npc for row in rows if row.health_fp > 300 and
            (row.away_until_world_minute is None or row.away_until_world_minute <= minute)}


@transaction.atomic
def submit_story_vote(payload: Mapping[str, Any], *, player_id=None):
    if not story_enabled():
        return _result(StoryResultCode.FEATURE_DISABLED)
    request_id = _uuid(payload.get("request_id"))
    if not request_id:
        return _result(StoryResultCode.ACTION_INVALID)
    expected = payload.get("expected_story_version", payload.get("params", {}).get("expected_story_version"))
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        return _result(StoryResultCode.VERSION_CONFLICT)
    params = dict(payload.get("params") or payload)
    allowed = STORY_COMMAND_PARAM_WHITELIST[StoryCommand.VOTE.value]
    if set(params) - allowed or not {"meeting_id", "plan_code"}.issubset(params):
        return _result(StoryResultCode.VOTE_INVALID)
    plan = str(params.get("plan_code", "")).upper()
    meeting_id = _uuid(params.get("meeting_id"))
    if not meeting_id or plan not in {p.value for p in StoryPlan}:
        return _result(StoryResultCode.VOTE_INVALID)
    digest = _hash(payload)
    with transaction.atomic():
        time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        from .caravan import sync_visit
        sync_visit(time.world_minute)
        arc, fact = _get_arc_locked(time.world_minute, request_id)
        replay = _existing_request(request_id, digest)
        if replay:
            return replay
        if expected is not None and arc.story_version != expected:
            return _result(StoryResultCode.VERSION_CONFLICT, current_story_version=arc.story_version)
        meeting = VillageStoryMeeting.objects.select_for_update().filter(meeting_id=meeting_id, arc=arc).first()
        error = meeting_state_error(meeting, time.world_minute, check_vote_deadline=True)
        if error:
            return _result(error, data={"meeting_id": str(meeting_id),
                "meeting_status": meeting.status if meeting else None,
                "starts_world_minute": meeting.starts_world_minute if meeting else None,
                "closes_day": meeting.closes_day if meeting else None})
        if plan not in PLAN_REQUIRED_FACT or plan not in meeting.candidate_plans:
            return _result(StoryResultCode.FACT_NOT_UNLOCKED if plan == StoryPlan.FOREST_ROUTE.value else StoryResultCode.VOTE_INVALID)
        if plan not in (meeting.proposals or {}):
            return _result("PUBLIC_PROPOSAL_REQUIRED")
        if int(meeting.proposals[plan].get("expires_world_minute", 0)) <= time.world_minute:
            return _result("PUBLIC_AUTHORIZATION_EXPIRED")
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
        readiness = meeting_readiness(meeting, time.world_minute, actor_id=voter_id if voter_type == "NPC" else None)
        result = _result(data={"meeting_id": str(meeting_id), "plan_code": plan, "story_version": arc.story_version,
            "vote_recorded": True, "replaced_previous_vote": previous is not None,
            **{key: readiness[key] for key in ("own_vote", "valid_npc_vote_count", "npc_vote_counts") if key in readiness}})
        return _record(request_id, digest, "STORY_VOTE", result,
                       actor=NpcDefinition.objects.filter(agent_id=voter_id).first() if voter_type == "NPC" else None,
                       players=[voter_id] if voter_type == "PLAYER" else [], world_minute=time.world_minute,
                       time_revision=time.time_revision, event_type="STORY_VOTE_CAST")


def _pick_plan(meeting, attendees, all_votes=None):
    all_votes = list(meeting.votes.all()) if all_votes is None else all_votes
    votes = {row.voter_id: row for row in all_votes if row.voter_type == "NPC" and row.is_valid}
    counts = {plan: 0 for plan in meeting.candidate_plans if plan in PLAN_REQUIRED_FACT}
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
    for row in all_votes:
        if row.voter_type == "PLAYER" and row.player_support and row.plan_code in winners:
            support[row.plan_code] += 1
    if support:
        supported = max(support.values())
        if supported:
            winners = [plan for plan in winners if support[plan] == supported]
    return next((plan for plan in SAFE_PLAN_PRIORITY if plan in winners), sorted(winners)[0])


def meeting_readiness(meeting, world_minute, *, actor_id=None, attendees=None):
    """Read-only view of the exact conditions used by settlement, not a decision.

    The caller doing settlement supplies locked attendees. Queries use ordinary
    reads, so inspecting progress cannot extend a meeting or require a write lock.
    """
    attendees = _attendees(world_minute, lock=False) if attendees is None else attendees
    votes = list(meeting.votes.all()) if meeting else []
    counts = {plan: 0 for plan in (meeting.candidate_plans if meeting else [])}
    for vote in votes:
        if vote.voter_type == "NPC" and vote.is_valid and vote.voter_id in attendees and vote.plan_code in counts:
            counts[vote.plan_code] += 1
    valid_count = sum(counts.values())
    plan = _pick_plan(meeting, attendees, votes) if meeting else None
    state_error = meeting_state_error(meeting, world_minute)
    reasons = []
    enabled = story_enabled()
    if not enabled:
        reasons.append("STORY_FEATURE_DISABLED")
    if state_error:
        reasons.append(state_error)
    if "10013" not in attendees:
        reasons.append("MARK_NOT_PRESENT")
    if len(attendees) < 3:
        reasons.append("NOT_ENOUGH_ATTENDEES")
    if valid_count < 3:
        reasons.append("NOT_ENOUGH_VALID_NPC_VOTES")
    if plan is None:
        reasons.append("NO_VOTED_PLAN")
    terms = (meeting.proposals or {}).get(plan) if meeting and plan else None
    if plan and not terms:
        reasons.append("PUBLIC_PROPOSAL_REQUIRED")
    elif terms and int(terms.get("expires_world_minute", 0)) <= world_minute:
        reasons.append("PUBLIC_AUTHORIZATION_EXPIRED")
    if not enabled:
        result_code = "STORY_FEATURE_DISABLED"
    elif state_error:
        result_code = state_error
    elif any(code in reasons for code in ("MARK_NOT_PRESENT", "NOT_ENOUGH_ATTENDEES", "NOT_ENOUGH_VALID_NPC_VOTES", "NO_VOTED_PLAN")):
        result_code = "STORY_NOT_ENOUGH_ATTENDANCE"
    else:
        result_code = reasons[0] if reasons else "OK"
    own = next((v for v in votes if v.voter_type == "NPC" and v.voter_id == str(actor_id)), None)
    data = {
        "meeting_id": str(meeting.pk) if meeting else None,
        "meeting_status": meeting.status if meeting else None,
        "location_code": "RECORD_HALL",
        "starts_world_minute": meeting.starts_world_minute if meeting else None,
        "closes_day": meeting.closes_day if meeting else None,
        "voting_open": meeting_state_error(meeting, world_minute, check_vote_deadline=True) is None,
        "voting_unavailable_reason": meeting_state_error(meeting, world_minute, check_vote_deadline=True),
        "mark_present": "10013" in attendees,
        "attendee_count": len(attendees), "required_attendee_count": 3,
        "valid_npc_vote_count": valid_count, "required_npc_vote_count": 3,
        "npc_vote_counts": counts,
        "leading_plan": plan,
        "can_settle": not reasons, "settlement_result_code": result_code,
        "unavailable_reasons": reasons,
    }
    if actor_id is not None:
        data["own_vote"] = None if own is None else {"plan_code": own.plan_code, "is_valid": own.is_valid,
            "counted": own.is_valid and own.voter_id in attendees and own.plan_code in counts,
            "submitted_world_minute": own.submitted_world_minute}
    return data


@transaction.atomic
def settle_story_meeting(meeting_id, expected_story_version=None, request_id=None, world_minute=None, *, record_failed_attempt=True, actor_id=None):
    if not story_enabled():
        return _result(StoryResultCode.FEATURE_DISABLED)
    time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
    world_minute = time.world_minute if world_minute is None else world_minute
    rid = _uuid(request_id) or uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:meeting:{meeting_id}:{world_minute // 1440}")
    arc = VillageStoryArc.objects.select_for_update().filter(arc_code=ARC_CODE).first()
    if arc is None:
        return _result(StoryResultCode.MEETING_NOT_FOUND, data={"meeting_id": str(meeting_id)})
    fact = VillageStoryFact.objects.select_for_update().get(arc=arc)
    meeting = VillageStoryMeeting.objects.select_for_update().filter(meeting_id=_uuid(meeting_id), arc=arc).first()
    error = meeting_state_error(meeting, world_minute)
    if error:
        data = meeting_readiness(meeting, world_minute, actor_id=actor_id)
        data["meeting_id"] = str(meeting_id)
        return _result(error, data=data)
    if expected_story_version is not None and arc.story_version != expected_story_version:
        return _result(StoryResultCode.VERSION_CONFLICT, current_story_version=arc.story_version)
    attendees = _attendees(world_minute)
    readiness = meeting_readiness(meeting, world_minute, actor_id=actor_id, attendees=attendees)
    meeting.attendance_count = len(attendees)
    plan = readiness["leading_plan"]
    if readiness["settlement_result_code"] == StoryResultCode.NOT_ENOUGH_ATTENDANCE.value:
        # A rejected Mark action rolls back its transaction. Return the actual
        # unchanged state rather than announcing a retry extension that won't persist.
        if not record_failed_attempt:
            return _result(StoryResultCode.NOT_ENOUGH_ATTENDANCE, data=readiness)
        meeting.failure_code = StoryResultCode.NOT_ENOUGH_ATTENDANCE.value
        if meeting.retry_count == 0:
            meeting.retry_count = 1
            meeting.closes_day += 1
        else:
            # No new public decision without the required meeting. Existing
            # approved finite rules keep their original expiry.
            meeting.status = "FAILED"
        meeting.save()
        return _result(StoryResultCode.NOT_ENOUGH_ATTENDANCE, data={**readiness,
            "retry_count": meeting.retry_count, "retry_day": meeting.closes_day, "status": meeting.status,
            "meeting_status": meeting.status, "closes_day": meeting.closes_day, "mark_required": True})
    if not readiness["can_settle"]:
        return _result(readiness["settlement_result_code"], data=readiness)
    terms = meeting.proposals[plan]
    meeting.status = "SETTLED"
    meeting.selected_plan = plan
    meeting.settled_day = world_minute // 1440
    meeting.save()
    decision = approve_decision(meeting, plan, world_minute)
    retry = False
    from_phase = arc.current_phase
    if not arc.is_completed:
        arc.current_phase = StoryPhase.EXECUTION.value
        arc.current_plan = plan
    arc.phase_entered_day = world_minute // 1440
    fact.last_progress_day = world_minute // 1440
    fact.save(update_fields=["last_progress_day", "updated_at"])
    if plan == StoryPlan.RATION_AND_WAIT.value:
        arc.ration_until_day = int(terms["expires_world_minute"]) // 1440
    else:
        arc.ration_until_day = None
    arc.story_version += 1
    arc.save()
    event = _event(event_type="STORY_MEETING_SETTLED", request_id=rid, world_minute=world_minute,
                   time_revision=time.time_revision, participants=list(attendees),
                   data={"meeting_id": str(meeting.pk), "plan_code": plan, "retry_fallback": retry,
                         "valid_npc_vote_count": readiness["valid_npc_vote_count"], "npc_vote_counts": readiness["npc_vote_counts"],
                         "attendee_count": readiness["attendee_count"], "decision": decision_data(decision)}, suffix="meeting")
    VillageStoryTransitionReceipt.objects.create(
        request_id=rid, arc=arc, from_phase=from_phase, to_phase=arc.current_phase,
        selected_plan=plan, source_day=world_minute // 1440, world_event=event,
        response_data={"retry_fallback": retry, "decision": decision_data(decision)},
    )
    return _result(data={"plan_code": plan, "decision": decision_data(decision), "retry_fallback": retry, "story_version": arc.story_version,
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
    from .phase3_asset_service import get_available_slot_amount
    minute = GlobalGameTimeState.objects.get(pk=1).world_minute
    def available(row):
        if not player_id:
            return get_available_slot_amount(row, minute)
        if row.freshness_unknown or (row.expires_world_minute is not None and row.expires_world_minute <= minute):
            return 0
        return row.item_amount
    if sum(available(row) for row in slots) < amount:
        return False
    remaining = amount
    for row in slots:
        take = min(remaining, available(row))
        if not take:
            continue
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
    from .phase3_asset_service import get_available_money
    if not bag or (bag.money if player_id else get_available_money(bag)) < amount:
        return None
    bag.money -= amount
    if player_id:
        bag.save(update_fields=["money"])
    else:
        bag.asset_version += 1
        bag.save(update_fields=["money", "asset_version"])
    return bag


def repair_labor_unavailable(runtime, world_minute, params):
    from .runtime_services import CommandService
    if not story_enabled():
        return StoryResultCode.FEATURE_DISABLED.value
    hours = params.get("labor_hours", 1)
    if type(hours) is not int or hours != 1:
        return StoryResultCode.ACTION_INVALID.value
    if runtime.action_status == "AWAY" or (
        runtime.away_until_world_minute is not None and runtime.away_until_world_minute > world_minute
    ):
        return StoryResultCode.FORBIDDEN_ACTOR.value
    error = CommandService.unavailable_reason("WORK_PLACEHOLDER", runtime)
    if error:
        return error
    arc = VillageStoryArc.objects.filter(arc_code=ARC_CODE).first()
    if not arc or arc.is_completed or arc.current_plan != "BRIDGE_REPAIR" or arc.current_phase != "EXECUTION":
        return StoryResultCode.PLAN_INVALID.value
    expected = params.get("expected_story_version")
    if expected is not None and expected != arc.story_version:
        return StoryResultCode.VERSION_CONFLICT.value
    if runtime.current_location_code not in STORY_ACTION_LOCATIONS["REPAIR_LABOR"]:
        return StoryResultCode.LOCATION_INVALID.value
    _, error = authorized_decision(params, "BRIDGE_REPAIR", world_minute)
    if error:
        return error
    return None


def complete_story_action(payload: Mapping[str, Any], *, player_id=None, _labor_command=None):
    # A normal returned business error must not commit a partial donation.
    with transaction.atomic():
        result = _complete_story_action(payload, player_id=player_id, _labor_command=_labor_command)
        if result.get("result_code") != "OK":
            transaction.set_rollback(True)
    return result


def _complete_story_action(payload: Mapping[str, Any], *, player_id=None, _labor_command=None):
    if not story_enabled():
        return _result(StoryResultCode.FEATURE_DISABLED)
    request_id = _uuid(payload.get("request_id"))
    if not request_id:
        return _result(StoryResultCode.ACTION_INVALID)
    digest = _hash(payload)
    params = dict(payload.get("params") or payload)
    action = str(payload.get("action_code", params.get("action_code", ""))).upper()
    expected = payload.get("expected_story_version", params.get("expected_story_version"))
    # Historical completed requests remain replayable after an action is retired.
    replay = _existing_request(request_id, digest)
    if replay:
        return replay
    if action in {"SCOUT_FOREST_ROUTE", "SUPPLY_FOREST_ROUTE"}:
        return _result("STORY_ACTION_RETIRED")
    if action not in ALL_STORY_ACTIONS or (expected is not None and
                                           (isinstance(expected, bool) or not isinstance(expected, int))):
        return _result(StoryResultCode.ACTION_INVALID)
    if (
        set(params) - STORY_COMMAND_PARAM_WHITELIST[StoryCommand.ACT.value]
        or "action_code" not in params
    ):
        return _result(StoryResultCode.ACTION_INVALID)
    with transaction.atomic():
        time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
        from .caravan import sync_visit
        sync_visit(time.world_minute)
        arc, fact = _get_arc_locked(time.world_minute, request_id)
        replay = _existing_request(request_id, digest)
        if replay:
            return replay
        if arc.is_completed and action not in ADMIN_ACTIONS | {"PUBLISH_RATION_RULES", "ENABLE_STORE_LIMITS", "CONTACT_EMERGENCY_CARAVAN", "PROVIDE_COMMUNITY_MEAL"}:
            return _result(StoryResultCode.ARC_COMPLETED)
        if expected is not None and arc.story_version != expected:
            return _result(StoryResultCode.VERSION_CONFLICT, current_story_version=arc.story_version)
        plan = arc.current_plan
        administrative = action in ADMIN_ACTIONS or action in {"PUBLISH_RATION_RULES", "ENABLE_STORE_LIMITS", "CONTACT_EMERGENCY_CARAVAN", "PROVIDE_COMMUNITY_MEAL"}
        continuing_authorized_work = action in {"PURCHASE_REPAIR_MATERIALS", "MANAGE_REPAIR_LEDGER"} and arc.current_phase == "PLAN_REVIEW"
        if not administrative and not continuing_authorized_work and arc.current_phase != StoryPhase.EXECUTION.value:
            return _result(StoryResultCode.PHASE_INVALID)
        if not administrative and plan not in STORY_ACTION_PLANS.get(action, set()):
            return _result(StoryResultCode.PLAN_INVALID)
        actor_id = str(payload.get("npc_id", params.get("npc_id", "")))
        actor = NpcDefinition.objects.filter(agent_id=actor_id, is_test_only=False).first() if actor_id else None
        player_actions = {
            StoryActionCode.DONATE_REPAIR_RESOURCES.value,
            StoryActionCode.PROVIDE_REPAIR_MEAL.value,
        }
        if player_id is not None and action not in player_actions:
            return _result(StoryResultCode.FORBIDDEN_ACTOR)
        if player_id is None and actor_id not in STORY_ACTION_ACTORS.get(action, set()):
            return _result(StoryResultCode.FORBIDDEN_ACTOR)
        location = str(payload.get("current_location_code", params.get("location_code", "")))
        if player_id is None and location not in STORY_ACTION_LOCATIONS.get(action, set()):
            if action == "CONTACT_EMERGENCY_CARAVAN":
                return _result(StoryResultCode.LOCATION_INVALID,
                               action_code=action, message="请回杂货铺开通")
            return _result(StoryResultCode.LOCATION_INVALID)
        if action in {"DONATE_REPAIR_RESOURCES", "REPAIR_LABOR"}:
            if arc.current_phase != StoryPhase.EXECUTION.value:
                return _result(StoryResultCode.PLAN_INVALID)
            if player_id is None:
                runtime = NpcRuntimeState.objects.select_for_update().filter(npc=actor).first()
                if runtime is None or runtime.health_fp <= 300 or runtime.action_status == "AWAY" or (
                    runtime.away_until_world_minute is not None and runtime.away_until_world_minute > time.world_minute
                ):
                    return _result(StoryResultCode.FORBIDDEN_ACTOR)
        if player_id is None:
            error = actor_error(actor_id, time.world_minute, dangerous=action in {"REPAIR_LABOR", "TRANSPORT_REPAIR_MATERIALS"},
                                active_command_id=_labor_command.command_id if _labor_command else None)
            if error:
                return _result(error)
        decision = None
        if action in {"PURCHASE_REPAIR_MATERIALS", "DONATE_REPAIR_RESOURCES", "REPAIR_LABOR", "PROVIDE_REPAIR_MEAL", "MANAGE_REPAIR_LEDGER"}:
            decision, error = authorized_decision(params, "BRIDGE_REPAIR", time.world_minute,
                                                   actor_id if action == "PURCHASE_REPAIR_MATERIALS" else None)
            if error:
                return _result(error)
        cost = {"gold": 0}
        changes = {"action_code": action}
        if decision:
            changes["decision_id"] = str(decision.pk)
        if action in ADMIN_ACTIONS or action == "PUBLISH_RATION_RULES":
            error, admin_changes = handle_admin(action, actor, arc, fact, params, time, request_id)
            if error:
                transaction.set_rollback(True)
                return _result(error, data={"action_code": action, **admin_changes})
            changes.update(admin_changes)
        elif action == StoryActionCode.ENABLE_STORE_LIMITS.value:
            decision, error = authorized_decision(params, "RATION_AND_WAIT", time.world_minute, actor_id)
            if error:
                return _result(error)
            if not fact.ration_rules_published:
                return _result("PUBLIC_DECISION_NOT_PUBLISHED")
            limits = dict(decision.terms["ration_daily_limits"])
            item_id = _int_value(params.get("limit_item_id"), 1)
            daily_limit = _int_value(params.get("daily_limit"), 1)
            expiry = int(decision.terms["expires_world_minute"])
            if item_id is None or daily_limit != limits.get(str(item_id)) or params.get("expires_world_minute", expiry) != expiry:
                return _result("PUBLIC_LIMIT_OUTSIDE_AUTHORIZATION")
            fact.ration_limits_until_world_minute = expiry
            fact.store_limits_enabled = True
            fact.ration_daily_limits = limits
            changes.update({"decision_id": str(decision.pk), "ration_limit": {"item_id": item_id, "daily_limit": daily_limit}, "expires_world_minute": expiry})
        elif action == StoryActionCode.REPAIR_LABOR.value:
            from .phase5_contract import REPAIR_LABOR_MINUTES, is_repair_labor
            command = _labor_command
            if command is None or not is_repair_labor(command.kind, command.sanitized_parameters) or (
                command.npc_id != actor.pk or command.request_id != request_id or command.status != "RUNNING"
                or command.started_world_minute is None or command.duration_minutes != REPAIR_LABOR_MINUTES
                or runtime.active_command_id != command.command_id
                or time.world_minute < command.started_world_minute + REPAIR_LABOR_MINUTES
            ):
                return _result(StoryResultCode.ACTION_INVALID, data={"reason": "修桥劳动必须通过已开始的60分钟命令完成"})
            error = repair_labor_unavailable(runtime, time.world_minute, params)
            if error:
                return _result(error)
            hours = params.get("labor_hours", 1)
            if type(hours) is not int or hours != 1:
                return _result(StoryResultCode.ACTION_INVALID)
            fact.repair_labor_hours += hours
            fact.bridge_condition = "REPAIRING"
            fact.public_trust = min(100, fact.public_trust + 1)
            changes["labor_hours"] = hours
        elif action == StoryActionCode.PROVIDE_COMMUNITY_MEAL.value:
            from .lucy_restaurant import list_meal
            result = list_meal(actor_id, {"store_code": "RESTAURANT_STORE", "item_id": params.get("meal_item_id"),
                "amount": params.get("meal_amount", 1), "price_mode": "ORIGINAL",
                "food_instance_id": params.get("food_instance_id"), "public_meal_code": params.get("public_meal_code"),
                "decision_id": params.get("decision_id"), "stock_source": params.get("stock_source", "PERSONAL")},
                time.world_minute, request_id)
            if result["result_code"] != "OK":
                transaction.set_rollback(True)
                return _result(result["result_code"])
            changes["public_meal_listing"] = result["data"]
        elif action == StoryActionCode.PROVIDE_REPAIR_MEAL.value:
            item_id = _int_value(params.get("meal_item_id", 29001), 1)
            amount = _int_value(params.get("meal_amount", 1), 1)
            if item_id is None or amount is None:
                return _result(StoryResultCode.ACTION_INVALID)
            if player_id is None and not _consume(actor_id, item_id, amount):
                return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
            if player_id is not None and not _consume("", item_id, amount, player_id):
                return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
            fact.community_meals += amount
            changes["meals"] = {"item_id": item_id, "amount": amount}
        elif action == StoryActionCode.DONATE_REPAIR_RESOURCES.value:
            item_id = params.get("item_id")
            item_id = None if item_id is None else _int_value(item_id, 1)
            amount = _int_value(params.get("amount", 0), 0)
            gold = _int_value(params.get("gold_amount", 0), 0)
            if amount is None or gold is None or (params.get("item_id") is not None and item_id is None):
                return _result(StoryResultCode.ACTION_INVALID)
            if item_id is None and gold <= 0:
                return _result(StoryResultCode.ACTION_INVALID)
            from .phase3_catalog import REPAIR_WOOD_ITEM_ID, REPAIR_METAL_ITEM_ID
            if item_id is not None and item_id not in (REPAIR_WOOD_ITEM_ID, REPAIR_METAL_ITEM_ID):
                return _result(StoryResultCode.ACTION_INVALID)
            if item_id is not None and (amount <= 0 or not _consume(actor_id, item_id, amount, player_id)):
                return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
            if item_id is not None:
                if item_id == REPAIR_WOOD_ITEM_ID:
                    fact.repair_wood += amount
                else:
                    fact.repair_metal += amount
                changes["item"] = {"item_id": item_id, "amount": amount}
            if gold:
                if not _money(actor_id, gold, player_id):
                    transaction.set_rollback(True)
                    return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
                fund = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
                fund.balance += gold
                fund.asset_version += 1
                fund.save(update_fields=["balance", "asset_version"])
                fact.repair_fund += gold
                changes["gold"] = gold
                changes["PUBLIC_FUND"] = {"money": gold}
                changes[actor_id if player_id is None else "PLAYER:" + str(player_id)] = {"money": -gold}
            fact.public_trust = min(100, fact.public_trust + 2)
        elif action == StoryActionCode.PURCHASE_REPAIR_MATERIALS.value:
            amount = _int_value(params.get("amount", 1), 1)
            item_id = _int_value(params.get("item_id", 4003), 1)
            if amount is None or item_id != 4003:
                return _result(StoryResultCode.ACTION_INVALID)
            cfg = _config()
            gold = amount * (cfg.repair_metal_unit_price if cfg else 50)
            if gold > int(decision.terms.get("budget_gold", 0)) - decision.spent_gold:
                return _result("PUBLIC_BUDGET_EXCEEDED", data={"remaining_budget": max(0, int(decision.terms.get("budget_gold", 0))-decision.spent_gold)})
            fund = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
            if fund.balance < gold:
                return _result(StoryResultCode.FUND_INSUFFICIENT)
            fund.balance -= gold
            fund.asset_version += 1
            fund.save(update_fields=["balance", "asset_version"])
            fact.repair_metal += amount
            decision.spent_gold += gold
            decision.save(update_fields=["spent_gold"])
            changes.update({"remaining_budget": int(decision.terms["budget_gold"])-decision.spent_gold, "gold": -gold, "item": {"item_id": 4003, "amount": amount},
                            "stock_destination": "REPAIR_PROJECT", "PUBLIC_FUND": {"money": -gold}, "REPAIR_PROJECT": {"4003": amount}})
        elif action == StoryActionCode.CONTACT_EMERGENCY_CARAVAN.value:
            attempt_id = _uuid(params.get("route_attempt_id"))
            if not attempt_id:
                return _result(StoryResultCode.ACTION_INVALID, data={
                    "failure_kind": "MISSING_ARGUMENT" if not params.get("route_attempt_id") else "INVALID_ARGUMENT",
                    "invalid_field": "route_attempt_id", "action_code": action,
                    "expected": "新的UUID；同一次业务尝试的重投须复用该UUID",
                })
            old = VillageRouteAttempt.objects.select_for_update().filter(route_attempt_id=attempt_id).first()
            if old:
                return _result(StoryResultCode.ROUTE_ATTEMPT_REPLAY, data={
                    "result_code": old.result_code,
                    "route_attempt_id": str(attempt_id),
                    "progress": old.progress,
                })
            frozen = {"rule": "NORA_FEE_ONLY_V1"}
            cfg = _config()
            seed = f"guaranteed-contact:{attempt_id}"
            success = True
            gold = cfg.caravan_contact_cost if cfg else 100
            from .caravan import validate_demand, save_demand
            demand = params.get("item_ids")
            if demand is not None:
                error = validate_demand(demand, cfg)
                if error:
                    return _result(error)
            already_open = fact.emergency_caravan_succeeded
            if already_open:
                gold = 0
            if gold is None:
                return _result(StoryResultCode.ACTION_INVALID)
            cost = {"gold": gold}
            if cost["gold"] and not _money(actor_id, cost["gold"]):
                return _result(StoryResultCode.RESOURCE_INSUFFICIENT)
            if demand is not None:
                save_demand(demand, time.world_minute)
            fact.emergency_caravan_contacted = True
            fact.emergency_caravan_succeeded = success
            attempt_result = "SUCCESS"
            from .caravan import projection
            visit = projection(time.world_minute)
            changes.update({"route_attempt_id": str(attempt_id), "route_result": attempt_result,
                "contact_cost": cost["gold"], "already_open": already_open, "route_open": success,
                "route_frozen": visit["route_frozen"], "arrival_world_minute": visit["next_arrival_world_minute"],
                "arrival_conditional": False})
            if demand is not None:
                changes["requested_items"] = [{"item_id": item_id} for item_id in sorted(set(demand))]
            injury = {}
            VillageRouteAttempt.objects.create(
                route_attempt_id=attempt_id, arc=arc, request_id=request_id,
                frozen_input=frozen, seed=seed, result_code=attempt_result,
                cost=cost, injury=injury, progress={"succeeded": success, "already_open": already_open},
                created_world_minute=time.world_minute,
            )
        elif action == StoryActionCode.TRANSPORT_REPAIR_MATERIALS.value:
            if actor_id != "10012":
                return _result(StoryResultCode.FORBIDDEN_ACTOR)
            cfg = _config()
            if fact.repair_wood < (cfg.story_repair_wood_target if cfg else 10) or fact.repair_metal < (cfg.story_repair_metal_target if cfg else 4):
                return _result(StoryResultCode.NOT_READY)
            fact.bridge_condition = "MATERIALS_READY"
        elif action == StoryActionCode.MANAGE_REPAIR_LEDGER.value:
            error, review = review_project(actor, arc, params, time.world_minute, request_id)
            if error:
                return _result(error)
            changes.update(review)
            fact.public_trust = min(100, fact.public_trust + 1)
        if action in {"MANAGE_REPAIR_LEDGER", "PUBLISH_DECISION", "PUBLISH_RATION_RULES", "REVEAL_STORY_FACT"}:
            from .public_finance import pay_daily_salary
            fund = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
            mark_inv = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id="10013")
            pay_daily_salary(fund, mark_inv, changes, time.world_minute, request_id)
        if action not in ADMIN_ACTIONS:
            fact.last_progress_day = time.world_minute // 1440
        fact = _project_facts(fact, arc, time.world_minute // 1440)
        arc.story_version += 1
        arc.save(update_fields=["story_version", "updated_at"])
        cfg = _config()
        if fact.repair_fund >= (cfg.story_repair_fund_target if cfg else 500) and fact.repair_wood >= (cfg.story_repair_wood_target if cfg else 10) and fact.repair_metal >= (cfg.story_repair_metal_target if cfg else 4) and fact.repair_labor_hours >= (cfg.story_repair_labor_target if cfg else 24):
            fact.bridge_condition = "REPAIRED"
            fact.save(update_fields=["bridge_condition", "updated_at"])
        snapshot = _snapshot(arc, fact)
        result = _result(data={"action_code": action, "story_version": arc.story_version, "snapshot": snapshot,
            "story_phase": arc.current_phase, "bridge_condition": fact.bridge_condition})
        if action in {"DONATE_REPAIR_RESOURCES", "PURCHASE_REPAIR_MATERIALS", "REPAIR_LABOR", "PROVIDE_REPAIR_MEAL", "TRANSPORT_REPAIR_MATERIALS"}:
            result["data"]["progress"] = snapshot["plan_progress"]
        result["data"].update({key: value for key, value in changes.items() if key in {
            "salary", "mark_salary", "remaining_budget", "decision", "meeting_id", "proposal", "starts_world_minute", "closes_day", "candidate_plans", "public_meal_listing", "stock_destination",
            "decision_id", "reviewed_entry_id", "evidence_request_id", "fact_code", "ration_limit", "expires_world_minute",
            "item", "gold", "labor_hours", "meals", "route_attempt_id", "route_result", "contact_cost", "already_open",
            "route_open", "route_frozen", "arrival_world_minute", "arrival_conditional", "requested_items",
        }})
        ledger = {"changes": changes}
        if action == StoryActionCode.CONTACT_EMERGENCY_CARAVAN.value:
            ledger["external_delta"] = -cost["gold"]
        elif action == StoryActionCode.PURCHASE_REPAIR_MATERIALS.value:
            ledger["external_delta"] = -gold
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
    if arc.current_phase == StoryPhase.AFTERMATH.value and (day - arc.start_day >= 1 or fact.food_stock_coverage_days < 4 or fact.tool_stock_coverage_days < 7):
        _advance(arc, fact, StoryPhase.SHORTAGE_VISIBLE.value, day, time, uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:{day}:shortage"))
    elif arc.current_phase == StoryPhase.SHORTAGE_VISIBLE.value and day >= arc.phase_entered_day + 1:
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
    # 二跑复盘（R3）：仍在 AFTERMATH 且无行动时，发一条"自主安排"通报（每世界日一条）。
    _maybe_notify_story_idle(arc, day, world_minute, time, source_request_id)
    return _result(data=_snapshot(arc, fact, arc.meetings.filter(status="OPEN").first()))


def get_store_purchase_limit(npc_id, item_id, world_minute, store_code="GENERAL_STORE"):
    """Combine active public rationing and the store's own daily limits."""
    from .models import VillageStoreAccount
    limits = []
    account = VillageStoreAccount.objects.filter(store_id=store_code).first()
    if account and account.limits_until_world_minute and world_minute < account.limits_until_world_minute:
        limit = (account.daily_limits or {}).get(str(item_id))
        if limit is not None:
            limits.append(int(limit))
    if story_enabled() and store_code == "GENERAL_STORE":
        fact = VillageStoryFact.objects.filter(arc_id=ARC_CODE, store_limits_enabled=True).first()
        active = fact and (fact.ration_limits_until_world_minute is None or world_minute < fact.ration_limits_until_world_minute)
        limit = (fact.ration_daily_limits or {}).get(str(item_id)) if active else None
        if limit is not None:
            limits.append(int(limit))
    if not limits:
        return None
    start = (int(world_minute) // 1440) * 1440
    used = 0
    for changes in EconomicLedgerEntry.objects.filter(
        entry_type="BUY_FROM_STORE", actor_npc__agent_id=str(npc_id),
        world_minute__gte=start, world_minute__lt=start + 1440,
    ).values_list("changes", flat=True):
        changes = changes or {}
        if "STORE:" + store_code in changes:
            used += int(changes.get(str(npc_id), {}).get(str(item_id), 0) or 0)
    return max(0, min(limits) - used)


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
