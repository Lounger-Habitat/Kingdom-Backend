import time
import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from account.models import GameAccount

from village.models import (
    EconomicLedgerEntry,
    NpcRuntimeState,
    NpcStoryFactAccess,
    PlayerStoryFactAccess,
    VillageAccount,
    VillageActionReceipt,
    VillageDeliveryOutbox,
    VillageSimulationConfig,
    VillageStoryArc,
    VillageStoryFact,
    VillageStoryMeeting,
    VillageStoryTransitionReceipt,
    VillageStoryVote,
    VillagePublicDecision,
    VillageWorldEvent,
)
from village.phase5_contract import StoryActionCode, StoryPhase, StoryPlan, StoryResultCode
from village.phase5_story import (
    bootstrap_story,
    complete_story_action,
    get_story_snapshot,
    process_day_close,
    settle_story_meeting,
    submit_story_vote,
)
from village.runtime_services import CommandService


class PhaseFiveStoryTest(TestCase):
    def test_wrong_emergency_opening_location_is_informational_and_durable(self):
        self.story_arc()
        runtime = NpcRuntimeState.objects.get(npc=self.npcs["10015"])
        runtime.current_location_code = "EMERGENCY_TRADE_POST"
        runtime.save(update_fields=["current_location_code"])
        command_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
        envelope_id = str(uuid.uuid4())
        content = {"schema_version": "village.command.v1", "id": command_id,
            "request_id": request_id, "name": "STORY_ACT", "params": {
                "action_code": "CONTACT_EMERGENCY_CARAVAN", "expected_story_version": 0,
                "route_attempt_id": str(uuid.uuid4())}, "queue_mode": "STRICT",
            "not_before_world_minute": 0, "expires_world_minute": 120,
            "ttl_ms": 120000, "duration_minutes": 1}
        ok, code, command = CommandService.accept_command(envelope_id=envelope_id, sender="10015",
            recipient="WuxiVillage", timestamp_ms=int(time.time() * 1000), content=content)
        self.assertFalse(ok)
        self.assertEqual(code, "STORY_LOCATION_INVALID")
        status = CommandService.serialize_status(command)
        self.assertEqual(status["status"], "FAILED")
        self.assertIsNotNone(status["receipt"])
        data = status["receipt"]["data"]
        self.assertEqual(data["message"], "请回杂货铺开通")
        self.assertNotIn("next_action", data)
        self.assertNotIn("next_action_params", data)
        self.assertEqual(VillageActionReceipt.objects.filter(command=command).count(), 1)

    def setUp(self):
        self.config, _ = VillageSimulationConfig.objects.update_or_create(
            config_key="default",
            defaults={
                "village_story_enabled": True,
                "story_latest_day": 25,
                "story_repair_fund_target": 500,
                "story_repair_wood_target": 10,
                "story_repair_metal_target": 4,
                "story_repair_labor_target": 24,
            },
        )
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 0
        self.clock.time_revision = 1
        self.clock.save()
        self.npcs = {}
        for npc_id in ("10011", "10012", "10013", "10014", "10015"):
            npc, _ = NpcDefinition.objects.get_or_create(agent_id=npc_id, defaults={"name": npc_id})
            self.npcs[npc_id] = npc
            NpcGlobalInventory.objects.update_or_create(npc=npc, defaults={"money": 1000})
            NpcGlobalInventorySlot.objects.filter(bag__npc=npc).delete()
            NpcRuntimeState.objects.update_or_create(
                npc=npc,
                defaults={
                    "current_location_code": "RECORD_HALL",
                    "current_point_code": "RECORD_HALL",
                    "action_status": "IDLE",
                },
            )
        VillageAccount.objects.update_or_create(account_code="PUBLIC_FUND", defaults={"balance": 0})

    def story_arc(self, phase=StoryPhase.EXECUTION.value, plan=StoryPlan.BRIDGE_REPAIR.value):
        arc = VillageStoryArc.objects.create(
            arc_code="BROKEN_TRADE_ROUTE_V1",
            current_phase=phase,
            current_plan=plan,
            start_day=0,
            phase_entered_day=0,
        )
        fact = VillageStoryFact.objects.create(arc=arc, last_progress_day=0)
        if phase == "EXECUTION" and plan in {"BRIDGE_REPAIR", "RATION_AND_WAIT"}:
            meeting = self.meeting(arc=arc, opened_day=0, closes_day=0, candidate_plans=[plan], status="SETTLED", selected_plan=plan)
            self.decision = VillagePublicDecision.objects.create(meeting=meeting, plan_code=plan, terms=self.proposal_terms(plan), approved_world_minute=0, published_world_minute=0)
            if plan == "RATION_AND_WAIT":
                fact.ration_rules_published = True
                fact.save(update_fields=["ration_rules_published"])
        return arc, fact

    def meeting(self, **kwargs):
        # Explicit approved-proposal fixtures; production never invents terms.
        kwargs.setdefault("proposals", {plan: self.proposal_terms(plan) for plan in kwargs["candidate_plans"]})
        return VillageStoryMeeting.objects.create(**kwargs)

    def proposal_terms(self, plan):
        return {"plan_code": plan, "budget_gold": 1000 if plan == "BRIDGE_REPAIR" else 0,
                "executor_npc_id": "10012" if plan == "FOREST_ROUTE" else "10015",
                "expires_world_minute": self.clock.world_minute + 3 * 1440,
                "purchase_item_ids": [4003], "ration_daily_limits": {"1030": 1}}

    def slot(self, npc_id, item_id, amount):
        bag = NpcGlobalInventory.objects.get(npc__agent_id=npc_id)
        return NpcGlobalInventorySlot.objects.create(
            bag=bag, slot_index=0, item_id=item_id, item_amount=amount,
        )

    def action(self, action_code, npc_id, version, **params):
        if hasattr(self, "decision"):
            params.setdefault("decision_id", str(self.decision.pk))
        return complete_story_action({
            "request_id": str(uuid.uuid4()),
            "npc_id": npc_id,
            "current_location_code": params.pop("location_code", "REPAIR_YARD"),
            "expected_story_version": version,
            "action_code": action_code,
            "params": {"action_code": action_code, "expected_story_version": version, **params},
        })

    def test_bootstrap_snapshot_is_wire_compatible_and_flag_off_is_safe(self):
        snapshot = bootstrap_story()
        self.assertEqual(snapshot["success"], True)
        data = snapshot["data"]
        self.assertEqual(data["schema_version"], "village.story-snapshot.v1")
        self.assertEqual(data["arc_code"], "BROKEN_TRADE_ROUTE_V1")
        self.assertEqual(data["phase"], "AFTERMATH")
        self.assertEqual(data["bridge_condition"], "BROKEN")
        event = VillageWorldEvent.objects.get(event_type="STORY_BOOTSTRAPPED")
        self.assertEqual(event.public_data["story_id"], "BROKEN_TRADE_ROUTE_V1")
        self.assertEqual(event.public_data["story_version"], 0)
        self.assertEqual(event.public_data["snapshot"]["bridge_condition"], "BROKEN")
        self.config.village_story_enabled = False
        self.config.save(update_fields=["village_story_enabled"])
        disabled = get_story_snapshot()
        self.assertEqual(disabled["result_code"], StoryResultCode.FEATURE_DISABLED.value)
        self.assertEqual(VillageStoryArc.objects.count(), 1)

    def test_story_idle_notice_is_broadcast_once_per_world_day(self):
        """二跑复盘 R3：AFTERMATH 无行动时，每世界日至多一条"自主安排"通报。"""
        from items.models import Item

        # 备足粮/工具，让 AFTERMATH 不因短缺提前推进（只按天数推进）。
        Item.objects.update_or_create(item_id=1003, defaults={"item_type": "食物"})
        for npc_id in ("10011", "10012", "10013", "10014", "10015"):
            self.slot(npc_id, 1003, 20)
        tool_bag = NpcGlobalInventory.objects.get(npc__agent_id="10012")
        NpcGlobalInventorySlot.objects.create(
            bag=tool_bag, slot_index=1, item_id=4001, item_amount=20, remaining_uses=20,
        )

        # bootstrap 时 AFTERMATH 无行动 → 当日一条通报，措辞是"通报"不是指令。
        bootstrap_story()
        notices = VillageWorldEvent.objects.filter(event_type="STORY_IDLE_NOTICE")
        self.assertEqual(notices.count(), 1)
        notice = notices.get()
        self.assertIn("由你自己安排", notice.hub_summary)
        self.assertIn("什么都不做也可以", notice.hub_summary)
        self.assertEqual(
            set(VillageDeliveryOutbox.objects.filter(event=notice).values_list("recipient", flat=True)),
            {"10011", "10012", "10013", "10014", "10015", "ENV_CLIENTS"},
        )

        # 同一世界日重复日结不重复发（幂等）。
        process_day_close(0, 1440)
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="STORY_IDLE_NOTICE").count(), 1)

        # 第 1 天即满足门槛并进入下一阶段，不再发空窗通报。
        process_day_close(1, 2 * 1440)
        arc = VillageStoryArc.objects.get(arc_code="BROKEN_TRADE_ROUTE_V1")
        self.assertNotEqual(arc.current_phase, "AFTERMATH")
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="STORY_IDLE_NOTICE").count(), 1)

    def test_unity_story_paths_return_the_frozen_snapshot(self):
        client = APIClient()
        client.force_authenticate(user=GameAccount.objects.create(username="phase5-api", password="x"))
        response = client.get("/api/village/story/snapshot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["schema_version"], "village.story-snapshot.v1")

    def test_hub_accept_uses_authoritative_runtime_location(self):
        self.story_arc()
        runtime = NpcRuntimeState.objects.get(npc=self.npcs["10011"])
        runtime.current_location_code = "REPAIR_YARD"
        runtime.save(update_fields=["current_location_code"])
        now_ms = int(time.time() * 1000)
        ok, result_code, command = CommandService.accept_command(
            envelope_id=str(uuid.uuid4()), sender="10011", recipient="WuxiVillage",
            timestamp_ms=now_ms,
            content={
                "schema_version": "village.command.v1",
                "id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()),
                "name": "STORY_ACT",
                "params": {"action_code": "DONATE_REPAIR_RESOURCES", "expected_story_version": 0, "gold_amount": 1, "decision_id": str(self.decision.pk)},
                "queue_mode": "STRICT",
                "not_before_world_minute": 0,
                "expires_world_minute": 120,
                "ttl_ms": 120_000,
                "duration_minutes": 1,
            },
        )
        self.assertTrue(ok, result_code)
        self.assertEqual(result_code, "OK")
        self.assertIsNotNone(command)
        self.assertEqual(command.status, "SUCCEEDED")
        self.assertEqual(VillageAccount.objects.get(account_code="PUBLIC_FUND").balance, 1)

    def test_historical_forest_facts_do_not_unlock_retired_route(self):
        PlayerStoryFactAccess.objects.create(player_id="player-1", fact_code="FOREST_OLD_ROUTE", unlocked_world_minute=0)
        bootstrap_story()
        arc = VillageStoryArc.objects.get()
        fact = VillageStoryFact.objects.get(arc=arc)
        self.assertFalse(fact.forest_route_known)
        access = NpcStoryFactAccess.objects.create(
            npc=self.npcs["10012"], fact_code="FOREST_OLD_ROUTE", unlocked_world_minute=0,
        )
        snapshot = get_story_snapshot()["data"]
        self.assertEqual(snapshot["facts"]["forest_route_known"], False)
        access.is_public = True
        access.save(update_fields=["is_public"])
        self.assertEqual(get_story_snapshot()["data"]["facts"]["forest_route_known"], False)
        self.assertTrue(NpcStoryFactAccess.objects.filter(pk=access.pk, is_public=True).exists())

    def test_meeting_tie_uses_player_support_then_safe_priority(self):
        arc, _ = self.story_arc(phase=StoryPhase.FIRST_COUNCIL.value, plan="")
        meeting = self.meeting(
            arc=arc, meeting_type="FIRST_COUNCIL", opened_day=0, closes_day=0,
            candidate_plans=["BRIDGE_REPAIR", "RATION_AND_WAIT"],
        )
        for index, (npc_id, plan) in enumerate({
            "10011": "BRIDGE_REPAIR", "10012": "RATION_AND_WAIT", "10013": "BRIDGE_REPAIR",
            "10014": "RATION_AND_WAIT",
        }.items()):
            result = submit_story_vote({
                "request_id": str(uuid.uuid4()), "npc_id": npc_id,
                "expected_story_version": 0,
                "params": {"meeting_id": str(meeting.meeting_id), "plan_code": plan, "expected_story_version": 0},
            })
            self.assertEqual(result["result_code"], "OK", index)
        player = submit_story_vote({
            "request_id": str(uuid.uuid4()), "expected_story_version": 0,
            "params": {"meeting_id": str(meeting.meeting_id), "plan_code": "RATION_AND_WAIT", "expected_story_version": 0},
        }, player_id="player-1")
        self.assertEqual(player["result_code"], "OK")
        settled = settle_story_meeting(meeting.meeting_id, expected_story_version=0, world_minute=0)
        self.assertEqual(settled["data"]["plan_code"], "RATION_AND_WAIT")

    def test_insufficient_attendance_cannot_invent_a_public_decision(self):
        arc, _ = self.story_arc(phase=StoryPhase.FIRST_COUNCIL.value, plan="")
        meeting = self.meeting(
            arc=arc, meeting_type="FIRST_COUNCIL", opened_day=0, closes_day=0,
            candidate_plans=["BRIDGE_REPAIR", "RATION_AND_WAIT"],
        )
        first = settle_story_meeting(meeting.meeting_id, expected_story_version=0, world_minute=0)
        self.assertEqual(first["result_code"], StoryResultCode.NOT_ENOUGH_ATTENDANCE.value)
        meeting.refresh_from_db()
        self.assertEqual(meeting.retry_count, 1)
        second = settle_story_meeting(meeting.meeting_id, expected_story_version=0, world_minute=1440)
        self.assertEqual(second["result_code"], StoryResultCode.NOT_ENOUGH_ATTENDANCE.value)
        arc.refresh_from_db()
        self.assertEqual(arc.current_plan, "")
        self.assertIsNone(arc.ration_until_day)
        self.assertEqual(VillagePublicDecision.objects.count(), 0)

    def test_ration_plan_opens_review_after_three_days(self):
        arc, _ = self.story_arc(phase=StoryPhase.FIRST_COUNCIL.value, plan="")
        meeting = self.meeting(
            arc=arc, meeting_type="FIRST_COUNCIL", opened_day=0, closes_day=0,
            candidate_plans=["RATION_AND_WAIT"],
        )
        for npc_id in ("10011", "10012", "10013"):
            result = submit_story_vote({
                "request_id": str(uuid.uuid4()), "npc_id": npc_id,
                "expected_story_version": 0,
                "params": {
                    "meeting_id": str(meeting.meeting_id),
                    "plan_code": "RATION_AND_WAIT",
                    "expected_story_version": 0,
                },
            })
            self.assertEqual(result["result_code"], "OK")
        settle_story_meeting(meeting.meeting_id, expected_story_version=0, world_minute=0)
        arc.refresh_from_db()
        self.assertEqual(arc.ration_until_day, 3)
        process_day_close(3, 3 * 1440)
        arc.refresh_from_db()
        self.assertEqual(arc.current_phase, StoryPhase.CRISIS_CHECK.value)

    def test_donation_is_atomic_idempotent_and_updates_ledger_relationship(self):
        arc, fact = self.story_arc()
        self.slot("10011", 1002, 12)
        request_id = uuid.uuid4()
        payload = {
            "request_id": str(request_id), "npc_id": "10011",
            "current_location_code": "REPAIR_YARD", "expected_story_version": 0,
            "action_code": StoryActionCode.DONATE_REPAIR_RESOURCES.value,
            "params": {"action_code": StoryActionCode.DONATE_REPAIR_RESOURCES.value,
                        "expected_story_version": 0, "item_id": 1002, "amount": 10, "decision_id": str(self.decision.pk)},
        }
        first = complete_story_action(payload)
        second = complete_story_action(payload)
        self.assertEqual(first["result_code"], "OK")
        self.assertTrue(second["replayed"])
        fact.refresh_from_db()
        self.assertEqual(fact.repair_wood, 10)
        self.assertEqual(NpcGlobalInventorySlot.objects.get(bag__npc__agent_id="10011").item_amount, 2)
        self.assertEqual(VillageActionReceipt.objects.filter(request_id=request_id).count(), 1)
        self.assertEqual(EconomicLedgerEntry.objects.filter(request_id=request_id).count(), 1)
        self.assertGreater(VillageWorldEvent.objects.filter(request_id=request_id).count(), 0)

    def test_invalid_purchase_does_not_spend_public_fund(self):
        arc, _ = self.story_arc()
        fund = VillageAccount.objects.get(account_code="PUBLIC_FUND")
        fund.balance = 500
        fund.save(update_fields=["balance"])
        result = self.action(
            StoryActionCode.PURCHASE_REPAIR_MATERIALS.value, "10015", 0,
            location_code="GENERAL_STORE", item_id=1003, gold_amount=500,
        )
        self.assertEqual(result["result_code"], StoryResultCode.ACTION_INVALID.value)
        fund.refresh_from_db()
        self.assertEqual(fund.balance, 500)
        self.assertEqual(EconomicLedgerEntry.objects.count(), 0)

    def test_route_attempt_cannot_be_rerolled(self):
        arc, fact = self.story_arc(plan=StoryPlan.FOREST_ROUTE.value)
        fact.forest_route_known = True
        fact.forest_route_scouted = 2
        fact.forest_route_supply = 1
        fact.save()
        attempt_id = uuid.uuid4()
        first = self.action(
            StoryActionCode.CONTACT_EMERGENCY_CARAVAN.value, "10015", 0,
            location_code="MARKET_STREET", route_attempt_id=str(attempt_id),
            segment_code="EAST", companion_npc_id="10012", gold_amount=10,
        )
        self.assertEqual(first["result_code"], "OK")
        arc.refresh_from_db()
        replay = self.action(
            StoryActionCode.CONTACT_EMERGENCY_CARAVAN.value, "10015", arc.story_version,
            location_code="MARKET_STREET", route_attempt_id=str(attempt_id),
            segment_code="EAST", companion_npc_id="10012", gold_amount=10,
        )
        self.assertEqual(replay["result_code"], StoryResultCode.ROUTE_ATTEMPT_REPLAY.value)
        self.assertEqual(VillageStoryVote.objects.count(), 0)

    def test_contact_always_succeeds_charges_server_price_and_submits_demand(self):
        from items.models import Item
        from village.models import VillageCaravanRequest
        Item.objects.update_or_create(item_id=1049, defaults={"item_name": "盐", "is_deleted": False})
        arc, fact = self.story_arc(plan=StoryPlan.FOREST_ROUTE.value)
        fact.forest_route_scouted = 2
        fact.forest_route_supply = 1
        fact.save()
        result = self.action(StoryActionCode.CONTACT_EMERGENCY_CARAVAN.value, "10015", 0,
            location_code="GENERAL_STORE", route_attempt_id=str(uuid.uuid4()), gold_amount=1, item_ids=[1049])
        self.assertEqual(result["result_code"], "OK")
        fact.refresh_from_db()
        self.assertTrue(fact.emergency_caravan_succeeded)
        self.assertEqual(NpcGlobalInventory.objects.get(npc__agent_id="10015").money, 900)
        self.assertEqual(NpcRuntimeState.objects.get(npc__agent_id="10012").health_fp, 1000)
        self.assertEqual(VillageCaravanRequest.objects.get(pk=0).item_ids, [1049])

    def test_repair_material_purchase_uses_server_unit_price(self):
        arc, fact = self.story_arc()
        VillageAccount.objects.filter(account_code="PUBLIC_FUND").update(balance=500)
        result = self.action(StoryActionCode.PURCHASE_REPAIR_MATERIALS.value, "10015", 0,
            location_code="GENERAL_STORE", item_id=4003, amount=4, gold_amount=1)
        self.assertEqual(result["result_code"], "OK")
        fact.refresh_from_db()
        self.assertEqual(fact.repair_metal, 4)
        self.assertEqual(VillageAccount.objects.get(account_code="PUBLIC_FUND").balance, 300)

    def test_retired_forest_progress_does_not_automatically_finish_story(self):
        arc, fact = self.story_arc(plan=StoryPlan.FOREST_ROUTE.value)
        fact.emergency_caravan_succeeded = True
        fact.public_trust = 5
        fact.last_progress_day = 1
        fact.save(update_fields=["emergency_caravan_succeeded", "public_trust", "last_progress_day"])
        result = process_day_close(2, 2 * 1440)
        arc.refresh_from_db()
        self.assertIsNone(result["data"]["outcome"])
        self.assertFalse(arc.is_completed)
        self.assertTrue(get_story_snapshot()["data"]["retired_plan"])

    def test_ration_limits_are_part_of_story_fact(self):
        arc, _ = self.story_arc(plan=StoryPlan.RATION_AND_WAIT.value)
        result = self.action(
            StoryActionCode.ENABLE_STORE_LIMITS.value, "10015", 0,
            location_code="GENERAL_STORE", limit_item_id=1030, daily_limit=1,
        )
        self.assertEqual(result["result_code"], "OK")
        snapshot = get_story_snapshot()["data"]
        self.assertEqual(snapshot["facts"]["ration_daily_limits"], {"1030": 1})

    def test_stalled_execution_enters_plan_review(self):
        arc, fact = self.story_arc()
        fact.last_progress_day = 0
        fact.save(update_fields=["last_progress_day"])
        process_day_close(2, 2 * 1440)
        arc.refresh_from_db()
        self.assertEqual(arc.current_phase, StoryPhase.PLAN_REVIEW.value)
        self.assertEqual(arc.review_count, 1)

    def test_day_close_reaches_single_deadline_outcome(self):
        arc, fact = self.story_arc(plan=StoryPlan.RATION_AND_WAIT.value)
        arc.ration_until_day = 2
        arc.save(update_fields=["ration_until_day"])
        fact.food_stock_coverage_days = 0
        fact.public_trust = 5
        fact.save(update_fields=["food_stock_coverage_days", "public_trust"])
        first = process_day_close(25, 25 * 1440)
        second = process_day_close(25, 25 * 1440)
        arc.refresh_from_db()
        self.assertTrue(arc.is_completed)
        self.assertEqual(arc.outcome, "PARTIAL_EVACUATION")
        self.assertEqual(VillageStoryArc.objects.count(), 1)
        self.assertEqual(VillageStoryTransitionReceipt.objects.filter(to_phase="PARTIAL_EVACUATION").count(), 1)
        self.assertEqual(second["data"]["outcome"], "PARTIAL_EVACUATION")

    def test_story_deadline_is_relative_to_nonzero_start_day(self):
        start_day = 739703
        self.clock.world_minute = start_day * 1440
        self.clock.save(update_fields=["world_minute"])
        bootstrap_story()

        result = process_day_close(start_day + 1, (start_day + 1) * 1440)
        arc = VillageStoryArc.objects.get()

        self.assertFalse(arc.is_completed)
        self.assertEqual(result["data"]["phase"], StoryPhase.SHORTAGE_VISIBLE.value)
