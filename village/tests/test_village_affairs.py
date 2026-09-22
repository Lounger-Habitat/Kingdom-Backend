import importlib
import time
import uuid
from types import SimpleNamespace

from django.apps import apps
from django.db import connection, transaction
from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from village.affairs import (AffairError, complete_labor, contribute, ensure_bridge_affair,
                             labor_unavailable, project_affairs, create_resource_affair)
from village.models import (EconomicLedgerEntry, NpcProfile, NpcRuntimeState, VillageAccount, VillageLocationDefinition,
    VillageAffair, VillageAffairContribution, VillagePublicDecision, VillageSimulationConfig,
    VillageStoryArc, VillageStoryFact, VillageStoryMeeting, VillageWorldEvent)
from village.phase5_story import complete_story_action, process_day_close
from village.public_finance import public_fund_delta
from village.runtime_services import ActionService, CommandService
from village.phase2_services import TickService


class VillageAffairTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={
            "village_story_enabled": True, "village_needs_enabled": True})
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 0
        self.clock.save()
        self.npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        self.bag, _ = NpcGlobalInventory.objects.update_or_create(npc=self.npc, defaults={"money": 1000})
        self.bag.slots.all().delete()
        self.runtime, _ = NpcRuntimeState.objects.update_or_create(npc=self.npc, defaults={
            "current_location_code": "REPAIR_YARD", "current_point_code": "REPAIR_YARD", "action_status": "IDLE",
            "health_fp": 1000, "energy_fp": 1000, "hunger_fp": 0})
        NpcProfile.objects.update_or_create(npc=self.npc, defaults={"display_name": "Bill", "occupation": "farmer",
            "home_location_code": "BILL_HOME", "work_location_code": "BILL_FARM", "social_location_code": "VILLAGE_SQUARE",
            "capabilities": ["STORY_ACT"]})
        self.arc = VillageStoryArc.objects.create(arc_code="BROKEN_TRADE_ROUTE_V1", current_phase="SHORTAGE_VISIBLE")
        self.fact = VillageStoryFact.objects.create(arc=self.arc)
        self.fund, _ = VillageAccount.objects.update_or_create(account_code="PUBLIC_FUND", defaults={"balance": 9000})
        VillageLocationDefinition.objects.update_or_create(location_code="VILLAGE_SQUARE", defaults={
            "display_name": "广场", "center_anchor_code": "test_square", "location_type": "PUBLIC", "is_active": True})

    def slot(self, item_id, amount, **kwargs):
        return NpcGlobalInventorySlot.objects.create(bag=self.bag, slot_index=self.bag.slots.count(),
            item_id=item_id, item_amount=amount, **kwargs)

    def action(self, code, rid=None, **params):
        return complete_story_action({"request_id": str(rid or uuid.uuid4()), "npc_id": self.npc.agent_id,
            "current_location_code": "REPAIR_YARD", "params": {"action_code": code, **params}})

    def prepare(self):
        self.slot(1002, 100)
        self.slot(29201, 20, quality=3, expires_world_minute=3000)
        self.assertEqual(self.action("DONATE_REPAIR_RESOURCES", item_id=1002, amount=100, gold_amount=500)["result_code"], "OK")
        self.assertEqual(self.action("PROVIDE_REPAIR_MEAL", meal_item_id=29201, meal_amount=20)["result_code"], "OK")
        return VillageAffair.objects.get(pk="BRIDGE_REPAIR")

    def test_real_preparation_without_meeting_and_public_balance_is_not_funding(self):
        row = ensure_bridge_affair(0, self.arc, self.fact)
        self.assertEqual(row.progress["gold"], 0)
        self.assertEqual(labor_unavailable(row), "AFFAIR_PREPARATION_INCOMPLETE")
        row = self.prepare()
        self.assertEqual(row.status, "ACTIVE")
        self.assertIsNone(labor_unavailable(row))
        self.assertEqual(VillageStoryMeeting.objects.count(), 0)
        self.bag.refresh_from_db(); self.fund.refresh_from_db(); self.fact.refresh_from_db()
        self.assertEqual((self.bag.money, self.fund.balance, self.fact.repair_fund), (500, 9000, 500))
        self.assertEqual(self.fact.repair_metal, 0)
        entry = EconomicLedgerEntry.objects.get(changes__gold=500)
        self.assertEqual(public_fund_delta(entry), 0)

    def test_bad_food_overpayment_and_failed_mixed_donation_roll_back(self):
        wood = self.slot(1002, 100)
        for item_id, kwargs in [(1030, {}), (29201, {"expires_world_minute": 0}),
                                 (29201, {"expires_world_minute": 100, "quality": 0}), (29201, {})]:
            slot = self.slot(item_id, 20, **kwargs)
            self.assertNotEqual(self.action("PROVIDE_REPAIR_MEAL", meal_item_id=item_id, meal_amount=20)["result_code"], "OK")
            slot.refresh_from_db(); self.assertEqual(slot.item_amount, 20)
            slot.delete()
        self.assertEqual(self.action("DONATE_REPAIR_RESOURCES", gold_amount=501)["result_code"], "PROJECT_NEED_FILLED")
        self.bag.money = 1; self.bag.save()
        self.assertNotEqual(self.action("DONATE_REPAIR_RESOURCES", item_id=1002, amount=50, gold_amount=500)["result_code"], "OK")
        wood.refresh_from_db(); self.assertEqual(wood.item_amount, 100)
        self.assertEqual(VillageAffairContribution.objects.count(), 0)

    def test_last_contribution_completes_once_and_other_project_is_independent(self):
        bridge = self.prepare()
        Item.objects.update_or_create(pk=1002, defaults={"item_name": "木材", "item_price": 5})
        well = create_resource_affair(code="REPAIR_WELL", title="修复水井", world_minute=0,
            requirements={"materials": {"1002": 2}, "meals": 0, "gold": 0, "labor": 1},
            locations=["VILLAGE_SQUARE"])
        contribute(well, "material", 2, item_id=1002, world_minute=1, request_id=uuid.uuid4(), actor_npc=self.npc)
        complete_labor(well, world_minute=61, request_id=uuid.uuid4(), player_id="42")
        well.refresh_from_db(); bridge.refresh_from_db()
        self.assertEqual((well.status, bridge.status, bridge.progress["labor"]), ("COMPLETED", "ACTIVE", 0))
        for i in range(20):
            rid = uuid.uuid4()
            complete_labor(bridge, world_minute=60 * (i + 1), request_id=rid, player_id=str(i % 2))
        complete_labor(bridge, world_minute=1200, request_id=rid, player_id="1")
        self.fact.refresh_from_db()
        self.assertEqual(self.fact.bridge_condition, "REPAIRED")
        self.assertEqual(bridge.progress["labor"], 20)
        self.assertEqual(VillageWorldEvent.objects.filter(dedupe_key="affair:BRIDGE_REPAIR:COMPLETED").count(), 1)
        self.assertEqual(VillageAffairContribution.objects.filter(affair=bridge, kind="labor").count(), 20)
        with self.assertRaises(AffairError):
            complete_labor(bridge, world_minute=1300, request_id=uuid.uuid4(), actor_npc=self.npc)

    def test_npc_timed_work_requires_full_preparation_and_real_sixty_minutes(self):
        def command():
            ok, code, row = CommandService.accept_command(envelope_id=str(uuid.uuid4()), sender=self.npc.agent_id,
                recipient="WuxiVillage", timestamp_ms=int(time.time() * 1000), content={
                    "schema_version": "village.command.v1", "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()),
                    "name": "STORY_ACT", "params": {"action_code": "REPAIR_LABOR", "affair_id": "BRIDGE_REPAIR"},
                    "queue_mode": "STRICT", "not_before_world_minute": 0, "expires_world_minute": 1000,
                    "ttl_ms": 120000, "duration_minutes": 1})
            self.assertTrue(ok, code)
            return row
        row = command()
        self.assertFalse(CommandService.start_command(row.command_id)[0])
        self.prepare()
        row = command()
        self.assertTrue(CommandService.start_command(row.command_id)[0])
        self.arc.current_plan = "RATION_AND_WAIT"; self.arc.is_completed = True; self.arc.save()
        TickService.advance({"request_id": str(uuid.uuid4()), "world_minute": 59, "time_revision": 60})
        self.assertFalse(ActionService.complete_action(row.command_id, row.request_id, True, "OK")[0])
        TickService.advance({"request_id": str(uuid.uuid4()), "world_minute": 60, "time_revision": 61})
        result = ActionService.complete_action(row.command_id, row.request_id, True, "OK")
        self.assertTrue(result[0], result)
        self.assertTrue(result[2].success, result[2].response_data)
        self.fact.refresh_from_db(); self.assertEqual(self.fact.repair_labor_hours, 1)

    def test_day_close_no_longer_creates_meetings_or_freezes_work_at_day_25(self):
        for day in (1, 2, 25, 30):
            process_day_close(day, day * 1440)
        self.arc.refresh_from_db()
        self.assertFalse(self.arc.is_completed)
        self.assertFalse(VillageStoryMeeting.objects.exists())
        self.assertEqual(self.action("DONATE_REPAIR_RESOURCES", gold_amount=10)["result_code"], "OK")

    def test_migration_preserves_finished_bridge_assets_and_partial_contributions(self):
        self.fact.repair_wood = 40; self.fact.community_meals = 3; self.fact.repair_labor_hours = 2
        self.fact.repair_fund = 9000; self.fact.save()
        module = importlib.import_module("village.migrations.0046_village_affairs")
        module.migrate_affairs(apps, SimpleNamespace(connection=connection))
        row = VillageAffair.objects.get(pk="BRIDGE_REPAIR")
        self.assertEqual(row.progress, {"materials": {"1002": 40}, "meals": 3, "gold": 0, "labor": 2})
        self.fund.refresh_from_db(); self.assertEqual(self.fund.balance, 9000)
        row.delete()
        self.fact.bridge_condition = "REPAIRED"; self.fact.save()
        module.migrate_affairs(apps, SimpleNamespace(connection=connection))
        self.assertEqual(VillageAffair.objects.get(pk="BRIDGE_REPAIR").status, "COMPLETED")

    def test_read_projection_does_not_create_affairs(self):
        data = project_affairs(0)
        self.assertEqual(data[0]["gold"]["submitted"], 0)
        self.assertEqual(VillageAffair.objects.count(), 0)

    def test_factory_validates_definitions_and_never_resets_existing_progress(self):
        row = create_resource_affair("CLEAN_SQUARE", "清理广场", {"labor": 2}, ["VILLAGE_SQUARE"], 0)
        self.assertEqual(row.status, "ACTIVE")
        complete_labor(row, world_minute=60, request_id=uuid.uuid4(), actor_npc=self.npc)
        same = create_resource_affair("CLEAN_SQUARE", "清理广场", {"labor": 2}, ["VILLAGE_SQUARE"], 60)
        self.assertEqual(same.progress["labor"], 1)
        with self.assertRaises(AffairError):
            create_resource_affair("BAD", "坏配置", {"gold": True}, ["VILLAGE_SQUARE"], 0)
        with self.assertRaises(AffairError):
            create_resource_affair("BAD", "坏配置", {"labor": 1}, ["UNKNOWN_LOCATION"], 0)

    def _public_actor(self, npc_id, location):
        actor, _ = NpcDefinition.objects.get_or_create(agent_id=npc_id, defaults={"name": npc_id})
        NpcGlobalInventory.objects.update_or_create(npc=actor, defaults={"money": 1000})
        NpcRuntimeState.objects.update_or_create(npc=actor, defaults={"current_location_code": location, "action_status": "IDLE"})
        return actor

    def _public_action(self, action, actor, location, **params):
        return complete_story_action({"request_id": str(uuid.uuid4()), "npc_id": actor.agent_id,
            "current_location_code": location, "params": {"action_code": action, **params}})

    def test_public_wood_procurement_obeys_authorization_budget_and_affair_remaining(self):
        nora = self._public_actor("10015", "GENERAL_STORE")
        Item.objects.update_or_create(pk=1002, defaults={"item_name": "木材", "item_price": 5})
        meeting = VillageStoryMeeting.objects.create(arc=self.arc, opened_day=0, closes_day=0, status="SETTLED", selected_plan="BRIDGE_REPAIR")
        decision = VillagePublicDecision.objects.create(meeting=meeting, plan_code="BRIDGE_REPAIR", approved_world_minute=0,
            published_world_minute=0, terms={"executor_npc_id": "10015", "budget_gold": 15,
                                           "expires_world_minute": 1000, "purchase_item_ids": [1002]})
        def purchase(**params):
            return self._public_action("PURCHASE_REPAIR_MATERIALS", nora, "GENERAL_STORE", decision_id=str(decision.pk), **params)
        self.assertEqual(purchase(item_id=4003, amount=1)["result_code"], "PUBLIC_ITEM_NOT_AUTHORIZED")
        self.assertEqual(purchase(item_id=1002, amount=2)["result_code"], "OK")
        self.assertEqual(purchase(item_id=1002, amount=2)["result_code"], "PUBLIC_BUDGET_EXCEEDED")
        decision.refresh_from_db(); self.fund.refresh_from_db(); self.fact.refresh_from_db()
        self.assertEqual((decision.spent_gold, self.fund.balance, self.fact.repair_wood, self.fact.repair_fund), (10, 8990, 2, 0))

    def test_meeting_and_cross_purpose_decisions_leave_other_affairs_running(self):
        mark = self._public_actor("10013", "RECORD_HALL")
        result = self._public_action("CALL_MEETING", mark, "RECORD_HALL")
        self.assertEqual(result["result_code"], "OK")
        self.arc.refresh_from_db(); self.assertEqual((self.arc.current_phase, self.arc.current_plan), ("SHORTAGE_VISIBLE", ""))
        row = ensure_bridge_affair(0)
        self.assertEqual(row.status, "ACTIVE")
        decisions = []
        for number, plan in enumerate(["BRIDGE_REPAIR", "RATION_AND_WAIT"]):
            meeting = VillageStoryMeeting.objects.create(arc=self.arc, meeting_type=plan, opened_day=number, closes_day=number,
                status="SETTLED", selected_plan=plan)
            decisions.append(VillagePublicDecision.objects.create(meeting=meeting, plan_code=plan,
                approved_world_minute=0, terms={"expires_world_minute": 1000, "ration_daily_limits": {"1030": 1}}))
        # Publish the later ration decision before the older bridge decision.
        for decision in reversed(decisions):
            self.assertEqual(self._public_action("PUBLISH_DECISION", mark, "RECORD_HALL", decision_id=str(decision.pk))["result_code"], "OK")
        for decision in decisions:
            decision.refresh_from_db(); self.assertIsNone(decision.revoked_world_minute)
        self.fact.refresh_from_db(); self.assertTrue(self.fact.store_limits_enabled)
