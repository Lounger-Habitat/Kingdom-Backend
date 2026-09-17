"""Meeting proposals, bounded public decisions, and evidence-based records.

Called under the story service's world/arc transaction. These are immediate
administrative actions; existing work duration configuration is unchanged.
"""
import copy
import uuid

from inventory.models import NpcDefinition, NpcGlobalInventory
from items.models import Item
from .models import (EconomicLedgerEntry, NpcRuntimeState, NpcStoryFactAccess,
                     VillageAccount, VillageProjectReview, VillagePublicDecision,
                     VillageStoryMeeting, VillageActionReceipt)
from .phase5_contract import FORMAL_NPC_IDS
from .public_finance import pay_daily_salary

ADMIN_ACTIONS = {"CALL_MEETING", "REGISTER_PROPOSAL", "PUBLISH_DECISION", "REVEAL_STORY_FACT", "CLAIM_PUBLIC_SALARY"}
PUBLIC_ACTIONS = ADMIN_ACTIONS | {"MANAGE_REPAIR_LEDGER", "PUBLISH_RATION_RULES", "ENABLE_STORE_LIMITS", "PURCHASE_REPAIR_MATERIALS"}


def as_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def integer(value, minimum=0):
    return value if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= 2**53-1 else None


def actor_error(actor_id, world_minute, *, dangerous=False, active_command_id=None):
    runtime = NpcRuntimeState.objects.select_for_update().filter(npc__agent_id=str(actor_id)).first()
    if runtime is None:
        return "STORY_FORBIDDEN_ACTOR"
    if runtime.action_status == "AWAY" or (runtime.away_until_world_minute is not None and runtime.away_until_world_minute > world_minute):
        return "NPC_AWAY"
    if runtime.health_fp <= 300 or runtime.action_status == "SLEEPING":
        return "MARK_UNAVAILABLE" if str(actor_id) == "10013" else "STORY_ACTOR_UNAVAILABLE"
    if runtime.active_command_id is not None and runtime.active_command_id != active_command_id:
        return "STORY_ACTOR_BUSY"
    if dangerous and (runtime.hunger_fp >= 900 or runtime.energy_fp <= 100):
        return "STORY_ACTOR_UNAVAILABLE"
    return None


def decision_data(row):
    return {"decision_id": str(row.pk), "meeting_id": str(row.meeting_id), "plan_code": row.plan_code,
            "terms": copy.deepcopy(row.terms), "spent_gold": row.spent_gold,
            "remaining_budget": max(0, int(row.terms.get("budget_gold", 0))-row.spent_gold),
            "published_world_minute": row.published_world_minute, "revoked_world_minute": row.revoked_world_minute}


def authorized_decision(params, plan, world_minute, actor_id=None, *, lock=True):
    key = as_uuid(params.get("decision_id"))
    rows = VillagePublicDecision.objects.select_related("meeting")
    if lock:
        rows = rows.select_for_update()
    row = rows.filter(pk=key).first() if key else None
    if not row or row.plan_code != plan or row.meeting.status != "SETTLED" or row.meeting.selected_plan != row.plan_code:
        return None, "PUBLIC_AUTHORIZATION_REQUIRED"
    if row.published_world_minute is None:
        return None, "PUBLIC_DECISION_NOT_PUBLISHED"
    if row.revoked_world_minute is not None or int(row.terms.get("expires_world_minute", 0)) <= world_minute:
        return None, "PUBLIC_AUTHORIZATION_EXPIRED"
    if actor_id is not None and str(row.terms.get("executor_npc_id")) != str(actor_id):
        return None, "STORY_FORBIDDEN_ACTOR"
    return row, None


def meeting_state_error(meeting, world_minute, *, check_start=True, check_vote_deadline=False):
    """Voting closes at the day boundary; an open meeting may still be settled.

    Settlement after the voting window is the existing day-close behavior.
    Querying this function never extends, closes or settles a meeting.
    """
    if meeting is None:
        return "STORY_MEETING_NOT_FOUND"
    if meeting.status == "SETTLED":
        return "STORY_MEETING_ALREADY_SETTLED"
    if meeting.status != "OPEN":
        return "STORY_MEETING_CLOSED"
    if check_vote_deadline and world_minute // 1440 > meeting.closes_day:
        return "STORY_MEETING_CLOSED"
    if check_start and meeting.starts_world_minute is not None and world_minute < meeting.starts_world_minute:
        return "STORY_MEETING_NOT_STARTED"
    return None


def register_proposal(arc, fact, params, world_minute):
    meeting = VillageStoryMeeting.objects.select_for_update().filter(pk=as_uuid(params.get("meeting_id")), arc=arc).first()
    error = meeting_state_error(meeting, world_minute, check_start=False, check_vote_deadline=True)
    if error:
        return error, {"meeting_id": str(params.get("meeting_id", ""))}
    plan = str(params.get("plan_code", ""))
    if plan not in {"BRIDGE_REPAIR", "RATION_AND_WAIT"} or plan not in meeting.candidate_plans:
        return "STORY_PLAN_INVALID", {}
    if meeting.votes.exists():
        return "PUBLIC_PROPOSAL_LOCKED", {}
    expiry = integer(params.get("expires_world_minute"), 1)
    budget = integer(params.get("budget_gold", 0))
    executor = str(params.get("executor_npc_id", "10015"))
    if expiry is None or expiry <= max(world_minute, meeting.starts_world_minute or 0) or budget is None:
        return "STORY_ACTION_INVALID", {}
    expected_executor = "10015"
    meal_service = params.get("meal_service")
    if executor != expected_executor or (plan != "BRIDGE_REPAIR" and budget != 0 and not (plan == "RATION_AND_WAIT" and meal_service is not None)):
        return "STORY_FORBIDDEN_ACTOR", {}
    terms = {"plan_code": plan, "budget_gold": budget, "executor_npc_id": executor, "expires_world_minute": expiry}
    if meal_service is not None:
        from .lucy_restaurant import validate_meal_terms
        meal_terms = validate_meal_terms(meal_service, world_minute, expiry, budget) if plan == "RATION_AND_WAIT" else None
        if meal_terms is None:
            return "PUBLIC_MEAL_INVALID_TERMS", {}
        terms["meal_service"] = meal_terms
    if plan == "BRIDGE_REPAIR":
        terms["purchase_item_ids"] = [4003]
    if plan == "RATION_AND_WAIT":
        limits = params.get("ration_daily_limits")
        if not isinstance(limits, dict) or not limits:
            return "STORY_ACTION_INVALID", {}
        clean = {}
        for key, limit in limits.items():
            if not str(key).isdigit() or integer(limit, 1) is None or not Item.objects.filter(item_id=int(key), is_deleted=False).exists():
                return "STORY_ACTION_INVALID", {}
            clean[str(int(key))] = limit
        terms["ration_daily_limits"] = clean
    proposals = dict(meeting.proposals or {})
    proposals[plan] = terms
    meeting.proposals = proposals
    meeting.save(update_fields=["proposals", "updated_at"])
    return None, {"meeting_id": str(meeting.pk), "proposal": terms}


def approve_decision(meeting, plan, world_minute):
    """Copy terms frozen before the first vote; publication cannot edit them."""
    return VillagePublicDecision.objects.create(
        meeting=meeting, plan_code=plan, terms=copy.deepcopy(meeting.proposals[plan]), approved_world_minute=world_minute,
    )


def handle_admin(action, actor, arc, fact, params, time, request_id):
    minute = time.world_minute
    if str(actor.agent_id) != "10013":
        return "STORY_FORBIDDEN_ACTOR", {}
    changes = {"action_code": action}
    if action == "CALL_MEETING":
        if arc.meetings.filter(status="OPEN").exists():
            return "PUBLIC_MEETING_ALREADY_OPEN", {}
        if arc.meetings.filter(meeting_type="PUBLIC_COUNCIL", opened_day=minute//1440).exists():
            return "PUBLIC_MEETING_ALREADY_OPEN", {}
        if not arc.is_completed and arc.current_phase == "AFTERMATH":
            return "STORY_PHASE_INVALID", {}
        start = integer(params.get("starts_world_minute", minute))
        if start is None or start < minute:
            return "STORY_ACTION_INVALID", {}
        from .phase5_story import _candidate_plans
        candidates = ["RATION_AND_WAIT"] if arc.is_completed else _candidate_plans(fact)
        meeting = VillageStoryMeeting.objects.create(
            arc=arc, meeting_type="PUBLIC_COUNCIL", opened_day=minute//1440,
            closes_day=start//1440, starts_world_minute=start, candidate_plans=candidates,
        )
        if not arc.is_completed:
            arc.current_phase = "PLAN_REVIEW" if arc.current_plan else "FIRST_COUNCIL"
            arc.phase_entered_day = minute//1440
            arc.save(update_fields=["current_phase", "phase_entered_day", "updated_at"])
        changes.update({"meeting_id": str(meeting.pk), "starts_world_minute": start, "closes_day": meeting.closes_day, "candidate_plans": candidates})
    elif action == "REGISTER_PROPOSAL":
        error, data = register_proposal(arc, fact, params, minute)
        if error:
            return error, data
        changes.update(data)
    elif action in {"PUBLISH_DECISION", "PUBLISH_RATION_RULES"}:
        row = VillagePublicDecision.objects.select_for_update().filter(pk=as_uuid(params.get("decision_id")), meeting__arc=arc).first()
        if row is None and params.get("meeting_id") and not params.get("decision_id"):
            from .phase5_story import settle_story_meeting
            meeting_id = as_uuid(params.get("meeting_id"))
            result = settle_story_meeting(meeting_id, world_minute=minute, request_id=request_id,
                                         record_failed_attempt=False, actor_id=str(actor.agent_id))
            if result["result_code"] != "OK":
                return result["result_code"], result.get("data", {})
            arc.refresh_from_db()
            fact.refresh_from_db()
            row = VillagePublicDecision.objects.select_for_update().get(meeting_id=meeting_id)
        if row is None or row.meeting.status != "SETTLED" or row.meeting.selected_plan != row.plan_code:
            return "PUBLIC_AUTHORIZATION_REQUIRED", {}
        if row.revoked_world_minute is not None or int(row.terms.get("expires_world_minute", 0)) <= minute:
            return "PUBLIC_AUTHORIZATION_EXPIRED", {}
        if action == "PUBLISH_RATION_RULES" and row.plan_code != "RATION_AND_WAIT":
            return "STORY_PLAN_INVALID", {}
        if row.published_world_minute is not None:
            return "PUBLIC_DECISION_ALREADY_PUBLISHED", {}
        # A newer settled decision cannot be replaced by publishing an older one.
        newest = VillagePublicDecision.objects.filter(meeting__arc=arc).order_by(
            "-approved_world_minute", "-meeting__created_at",
        ).first()
        if newest.pk != row.pk:
            return "PUBLIC_AUTHORIZATION_EXPIRED", {}
        row.published_world_minute = minute
        row.save(update_fields=["published_world_minute"])
        VillagePublicDecision.objects.filter(meeting__arc=arc, revoked_world_minute__isnull=True).exclude(pk=row.pk).update(revoked_world_minute=minute)
        if row.plan_code == "RATION_AND_WAIT":
            fact.ration_rules_published = True
            # An announced public restriction is immediately the legal floor;
            # Nora cannot leave a purchase window by delaying her store action.
            fact.ration_daily_limits = dict(row.terms["ration_daily_limits"])
            fact.ration_limits_until_world_minute = int(row.terms["expires_world_minute"])
            fact.store_limits_enabled = True
        else:
            fact.ration_rules_published = False
            fact.store_limits_enabled = False
        changes["decision"] = decision_data(row)
    elif action == "REVEAL_STORY_FACT":
        code = str(params.get("fact_code", ""))
        access = NpcStoryFactAccess.objects.select_for_update().filter(npc=actor, fact_code=code).first()
        if code != "DEFERRED_BRIDGE_BUDGET" or access is None:
            return "STORY_FACT_NOT_UNLOCKED", {}
        if access.is_public:
            return "PUBLIC_FACT_ALREADY_PUBLISHED", {}
        access.is_public = True
        access.save(update_fields=["is_public"])
        fact.mark_budget_delay_revealed = True
        changes["fact_code"] = code
    elif action == "CLAIM_PUBLIC_SALARY":
        account = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
        inv = NpcGlobalInventory.objects.select_for_update().get(npc=actor)
        pay_daily_salary(account, inv, changes, minute, request_id, earn=False)
        if not changes["mark_salary"]:
            return ("STORY_FUND_INSUFFICIENT" if changes["salary"]["outstanding"] else "PUBLIC_NO_SALARY_DUE"), changes
    return None, changes


def review_project(actor, arc, params, world_minute, request_id):
    decision, error = authorized_decision(params, "BRIDGE_REPAIR", world_minute)
    if error:
        return error, {}
    entry = EconomicLedgerEntry.objects.select_for_update().filter(request_id=as_uuid(params.get("evidence_request_id"))).first()
    allowed = {"DONATE_REPAIR_RESOURCES", "PURCHASE_REPAIR_MATERIALS", "REPAIR_LABOR", "PROVIDE_REPAIR_MEAL"}
    if not entry or (entry.changes or {}).get("action_code") not in allowed:
        return "PUBLIC_EVIDENCE_INVALID", {}
    if str((entry.changes or {}).get("decision_id", "")) != str(decision.pk):
        return "PUBLIC_EVIDENCE_INVALID", {}
    if not VillageActionReceipt.objects.filter(request_id=entry.request_id, success=True).exists():
        return "PUBLIC_EVIDENCE_INVALID", {}
    if VillageProjectReview.objects.filter(entry=entry).exists():
        return "PUBLIC_EVIDENCE_ALREADY_REVIEWED", {}
    VillageProjectReview.objects.create(entry=entry, reviewer=actor, request_id=request_id, reviewed_world_minute=world_minute)
    return None, {"decision_id": str(decision.pk), "reviewed_entry_id": entry.id, "evidence_request_id": str(entry.request_id)}


def governance_query(arc, actor_id, world_minute):
    from .phase5_story import meeting_readiness
    meetings = [{"meeting_id": str(m.pk), "starts_world_minute": m.starts_world_minute,
                 "closes_day": m.closes_day, "candidate_plans": m.candidate_plans,
                 "proposals": m.proposals, "terms_locked": m.votes.exists(),
                 **meeting_readiness(m, world_minute, actor_id=actor_id)}
                for m in arc.meetings.filter(status="OPEN").order_by("opened_day")]
    decision_rows = VillagePublicDecision.objects.filter(meeting__arc=arc).order_by("-approved_world_minute", "-meeting__created_at")
    decisions = [decision_data(row) for row in decision_rows[:10]]
    data = {"meetings": meetings, "public_decisions": decisions,
            "public_decisions_scope": "LATEST_10", "has_more_public_decisions": decision_rows.count() > len(decisions),
            "mark_required_for_new_decision": True, "approved_work_may_continue_while_mark_away": True}
    if str(actor_id) == "10013":
        data["unreviewed_project_entries_scope"] = "LATEST_20"
        data["unreviewed_project_entries"] = [{"request_id": str(e.request_id), "entry_id": e.id,
                                               "changes": e.changes} for e in EconomicLedgerEntry.objects.filter(
            entry_type="STORY_STORY_ACT", project_review__isnull=True,
            changes__action_code__in=["DONATE_REPAIR_RESOURCES", "PURCHASE_REPAIR_MATERIALS", "REPAIR_LABOR", "PROVIDE_REPAIR_MEAL"],
        ).order_by("-id")[:20]]
    return data
