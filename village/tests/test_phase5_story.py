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
    VillageSimulationConfig,
    VillageStoryArc,
    VillageStoryFact,
    VillageStoryMeeting,
    VillageStoryTransitionReceipt,
    VillageStoryVote,
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
        return arc, fact

    def slot(self, npc_id, item_id, amount):
        bag = NpcGlobalInventory.objects.get(npc__agent_id=npc_id)
        return NpcGlobalInventorySlot.objects.create(
            bag=bag, slot_index=0, item_id=item_id, item_amount=amount,
        )

    def action(self, action_code, npc_id, version, **params):
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
        self.config.village_story_enabled = False
        self.config.save(update_fields=["village_story_enabled"])
        disabled = get_story_snapshot()
        self.assertEqual(disabled["result_code"], StoryResultCode.FEATURE_DISABLED.value)
        self.assertEqual(VillageStoryArc.objects.count(), 1)

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
                "params": {"action_code": "REPAIR_LABOR", "expected_story_version": 0, "labor_hours": 1},
                "queue_mode": "STRICT",
                "not_before_world_minute": 0,
                "expires_world_minute": 120,
                "ttl_ms": 120_000,
                "duration_minutes": 1,
            },
        )
        self.assertTrue(ok)
        self.assertEqual(result_code, "OK")
        self.assertIsNone(command)
        self.assertEqual(VillageStoryFact.objects.get().repair_labor_hours, 1)

    def test_npc_fact_can_unlock_route_but_player_private_fact_cannot(self):
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
        self.assertEqual(get_story_snapshot()["data"]["facts"]["forest_route_known"], True)

    def test_meeting_tie_uses_player_support_then_safe_priority(self):
        arc, _ = self.story_arc(phase=StoryPhase.FIRST_COUNCIL.value, plan="")
        meeting = VillageStoryMeeting.objects.create(
            arc=arc, meeting_type="FIRST_COUNCIL", opened_day=0, closes_day=0,
            candidate_plans=["BRIDGE_REPAIR", "FOREST_ROUTE", "RATION_AND_WAIT"],
        )
        for index, (npc_id, plan) in enumerate({
            "10011": "BRIDGE_REPAIR", "10012": "FOREST_ROUTE", "10013": "BRIDGE_REPAIR",
            "10014": "FOREST_ROUTE", "10015": "RATION_AND_WAIT",
        }.items()):
            result = submit_story_vote({
                "request_id": str(uuid.uuid4()), "npc_id": npc_id,
                "expected_story_version": 0,
                "params": {"meeting_id": str(meeting.meeting_id), "plan_code": plan, "expected_story_version": 0},
            })
            self.assertEqual(result["result_code"], "OK", index)
        player = submit_story_vote({
            "request_id": str(uuid.uuid4()), "expected_story_version": 0,
            "params": {"meeting_id": str(meeting.meeting_id), "plan_code": "FOREST_ROUTE", "expected_story_version": 0},
        }, player_id="player-1")
        self.assertEqual(player["result_code"], "OK")
        settled = settle_story_meeting(meeting.meeting_id, expected_story_version=0, world_minute=0)
        self.assertEqual(settled["data"]["plan_code"], "FOREST_ROUTE")

    def test_insufficient_attendance_retries_then_falls_back_to_ration(self):
        arc, _ = self.story_arc(phase=StoryPhase.FIRST_COUNCIL.value, plan="")
        meeting = VillageStoryMeeting.objects.create(
            arc=arc, meeting_type="FIRST_COUNCIL", opened_day=0, closes_day=0,
            candidate_plans=["BRIDGE_REPAIR", "RATION_AND_WAIT"],
        )
        first = settle_story_meeting(meeting.meeting_id, expected_story_version=0, world_minute=0)
        self.assertEqual(first["result_code"], StoryResultCode.NOT_ENOUGH_ATTENDANCE.value)
        meeting.refresh_from_db()
        self.assertEqual(meeting.retry_count, 1)
        second = settle_story_meeting(meeting.meeting_id, expected_story_version=0, world_minute=1440)
        self.assertEqual(second["data"]["plan_code"], "RATION_AND_WAIT")
        arc.refresh_from_db()
        self.assertEqual(arc.current_plan, "RATION_AND_WAIT")
        self.assertEqual(arc.ration_until_day, 3)

    def test_ration_plan_opens_review_after_three_days(self):
        arc, _ = self.story_arc(phase=StoryPhase.FIRST_COUNCIL.value, plan="")
        meeting = VillageStoryMeeting.objects.create(
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
        self.slot("10011", 1003, 12)
        request_id = uuid.uuid4()
        payload = {
            "request_id": str(request_id), "npc_id": "10011",
            "current_location_code": "REPAIR_YARD", "expected_story_version": 0,
            "action_code": StoryActionCode.DONATE_REPAIR_RESOURCES.value,
            "params": {"action_code": StoryActionCode.DONATE_REPAIR_RESOURCES.value,
                        "expected_story_version": 0, "item_id": 1003, "amount": 10},
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

    def test_forest_success_with_low_public_account_is_controlled_trade(self):
        arc, fact = self.story_arc(plan=StoryPlan.FOREST_ROUTE.value)
        fact.emergency_caravan_succeeded = True
        fact.public_trust = 5
        fact.last_progress_day = 1
        fact.save(update_fields=["emergency_caravan_succeeded", "public_trust", "last_progress_day"])
        result = process_day_close(2, 2 * 1440)
        arc.refresh_from_db()
        self.assertEqual(result["data"]["outcome"], "CONTROLLED_TRADE")
        self.assertTrue(arc.is_completed)

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

        result = process_day_close(start_day + 3, (start_day + 3) * 1440)
        arc = VillageStoryArc.objects.get()

        self.assertFalse(arc.is_completed)
        self.assertEqual(result["data"]["phase"], StoryPhase.SHORTAGE_VISIBLE.value)
