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
from .affairs import (AffairError, RESOURCE_ACTIONS, ensure_bridge_affair, get_affair,
                      contribute, complete_labor, labor_unavailable, validate_contribution, sync_bridge_fact, resolve_condition)
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
    from .affairs import project_affairs
    affairs = project_affairs(minute)
    available_actions = set(ADMIN_ACTIONS) | {"CONTACT_EMERGENCY_CARAVAN"}
    for affair in affairs:
        if affair["kind"] != "RESOURCE_PROJECT" or affair["status"] == "COMPLETED":
            continue
        if any(row["available"] and row["type"] in {"item", "gold"} for row in affair["conditions"]):
            available_actions.add("DONATE_REPAIR_RESOURCES")
        if any(row["available"] and row["id"] == "legacy_meals" for row in affair["conditions"]):
            available_actions.add("PROVIDE_REPAIR_MEAL")
        if affair["labor_available"]:
            available_actions.add("REPAIR_LABOR")
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
        "available_actions": sorted(available_actions),
        "affairs": affairs,
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
    from .models import VillageAffair
    affair = VillageAffair.objects.filter(pk="BRIDGE_REPAIR").first()
    if affair:
        sync_bridge_fact(affair, fact)
    slots = NpcGlobalInventorySlot.objects.filter(bag__npc__agent_id__in=FORMAL_NPC_IDS, item_amount__gt=0)
    tools = slots.filter(item_id__in=(4001, 4002))
    tool_uses = sum((row.remaining_uses if row.remaining_uses is not None else row.item_amount) for row in tools)
    food_ids = set(Item.objects.filter(item_type__in=("食物", "Food", "food")).values_list("item_id", flat=True))
    food_ids.add(29001)
    food_amount = sum(row.item_amount for row in slots.filter(item_id__in=food_ids))
    fact.tool_stock_coverage_days = Decimal(str(round(tool_uses / 2, 2)))
    fact.food_stock_coverage_days = Decimal(str(round(food_amount / 5, 2)))
    fact.save()
    return fact


def _candidate_plans(fact):
    plans = [StoryPlan.BRIDGE_REPAIR.value, StoryPlan.RATION_AND_WAIT.value]
    return plans




def bootstrap_story(request_id=None):
    return get_story_snapshot()


def get_story_snapshot():
    """Compatibility presentation envelope; reads never bootstrap a storyline."""
    from .affairs import project_affairs
    from .caravan import current_route
    minute = GlobalGameTimeState.objects.get(pk=1).world_minute
    affairs = project_affairs(minute)
    bridge = next((row for row in affairs if row["affair_id"] == "BRIDGE_REPAIR"), None)
    fact = VillageStoryFact.objects.filter(arc_id=ARC_CODE).first()
    bridge_condition = bridge["bridge_condition"] if bridge else fact.bridge_condition if fact else "BROKEN"
    return _result(data={"schema_version": "village.story-snapshot.v1", "arc_code": ARC_CODE,
        "phase": "AFFAIRS", "current_phase": "AFFAIRS", "plan": None, "outcome": None,
        "story_version": sum(row["version"] for row in affairs), "is_completed": False,
        "bridge_condition": bridge_condition, "meeting_open": False, "meeting_id": None,
        "affairs": affairs, "available_actions": ["CONTRIBUTE_TO_AFFAIR"] if any(row["available"] for row in affairs) else [],
        "facts": {"bridge_condition": bridge_condition, "emergency_caravan_succeeded": current_route() != "NONE"},
        "plan_progress": {}, "world_minute": minute})


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


def submit_story_vote(payload: Mapping[str, Any], *, player_id=None):
    return _result("ACTION_RETIRED")


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


def settle_story_meeting(meeting_id, expected_story_version=None, request_id=None, world_minute=None, *, record_failed_attempt=True, actor_id=None):
    return _result("ACTION_RETIRED")


def _consume(npc_id, item_id, amount, player_id=None, *, qualified_meal=False, instance_id=None, condition=None):
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
        if instance_id is not None and str(row.instance_id) != str(instance_id):
            return 0
        if qualified_meal and (row.quality < 1 or row.freshness_unknown or row.expires_world_minute is None or row.expires_world_minute <= minute):
            return 0
        if condition:
            if row.quality < condition.get("min_quality", 0):
                return 0
            minimum = condition.get("min_remaining_minutes", 0)
            if (condition.get("requires_freshness") or minimum > 0) and (
                row.freshness_unknown or row.expires_world_minute is None or row.expires_world_minute <= minute
                or row.expires_world_minute - minute < minimum
            ):
                return 0
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
    if isinstance(value, bool):
        return None
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
    try:
        affair = get_affair(params.get("affair_id"), world_minute, arc=arc)
        condition = resolve_condition(affair, params.get("condition_id"), "labor")
    except AffairError as exc:
        return exc.code
    if condition["locations"] and runtime.current_location_code not in condition["locations"]:
        return StoryResultCode.LOCATION_INVALID.value
    return None


def complete_story_action(payload: Mapping[str, Any], *, player_id=None, _labor_command=None):
    # A normal returned business error must not commit a partial donation.
    return _result("ACTION_RETIRED")






@transaction.atomic
def process_day_close(day_index: int, world_minute: int, source_request_id=None):
    time = GlobalGameTimeState.objects.select_for_update().get(pk=1)
    _return_away(time)
    from .affairs import project_affairs
    return _result(data={"affairs": project_affairs(world_minute)})


def get_store_purchase_limit(npc_id, item_id, world_minute, store_code="GENERAL_STORE", *, player_id=None):
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
    entries = EconomicLedgerEntry.objects.filter(
        entry_type="BUY_FROM_STORE", world_minute__gte=start, world_minute__lt=start + 1440)
    identity = str(npc_id)
    if player_id is None:
        entries = entries.filter(actor_npc__agent_id=str(npc_id))
    else:
        identity = "PLAYER:" + str(player_id)
        entries = entries.filter(actor_npc__isnull=True)
    for changes in entries.values_list("changes", flat=True):
        changes = changes or {}
        if "STORE:" + store_code in changes:
            used += int(changes.get(identity, {}).get(str(item_id), 0) or 0)
    return max(0, min(limits) - used)




def _return_away(time):
    rows = NpcRuntimeState.objects.select_for_update().filter(away_until_world_minute__isnull=False, away_until_world_minute__lte=time.world_minute).select_related("npc")
    for row in rows:
        row.away_until_world_minute = None
        row.save(update_fields=["away_until_world_minute", "updated_at"])
        _event(event_type="NPC_RETURNED", request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"phase5:return:{row.npc.agent_id}:{time.world_minute // 1440}"), world_minute=time.world_minute, time_revision=time.time_revision, actor=row.npc, participants=[row.npc.agent_id], data={"npc_id": str(row.npc.agent_id)}, suffix=f"return:{row.npc.agent_id}")


def dispatch_story_command(payload: Mapping[str, Any], *, player_id=None):
    return _result("ACTION_RETIRED", data={"replacement": "INSPECT_AFFAIRS / CONTRIBUTE_TO_AFFAIR"})


# Stable aliases used by Env/Hub adapters.
commit_story_action = complete_story_action
settle_story_action = complete_story_action
