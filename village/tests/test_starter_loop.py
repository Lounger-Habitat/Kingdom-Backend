"""Cross-service G1 loop through the authenticated player business boundary."""
import copy

from django.test import TestCase
from django.db.models import Max

from inventory.models import InventorySlot
from player.models import PlayerState
from village.player_actions import execute_player_action
from village.public_finance import reconcile
from village.models import EconomicLedgerEntry, VillageAccount
from . import test_player_gathering as gathering_fixture


class StarterLoopTests(TestCase):
    def setUp(self):
        # Same audited starting funds/AP and real legacy pool shape, with a
        # deterministic small catalog rather than random full-world fixtures.
        gathering_fixture.PlayerGatheringTests.setUp(self)
        VillageAccount.objects.filter(account_code="PUBLIC_FUND").update(audit_opening_balance=1000,
            audit_after_ledger_id=EconomicLedgerEntry.objects.aggregate(n=Max("id"))["n"] or 0)

    def act(self, action, params=None, location="", payload=None):
        import uuid
        data = payload or dict(player_id=str(self.player.pk), request_id=str(uuid.uuid4()),
            action=action, params=params or {}, current_location_code=location)
        answer = execute_player_action(data)
        self.assertTrue(answer["success"], answer)
        return answer, data

    def test_first_supply_investment_level_up_and_targeted_production_are_replay_safe(self):
        first, _ = self.act("collect", {"collect_type": "采集食材"}, "FOREST_EDGE")
        starter = first["data"]["overview"]["starter"]
        self.assertEqual(starter["supply"]["orders"][0]["reward_business_xp"], 20)
        order_id = starter["supply"]["orders"][0]["order_id"]
        accepted, _ = self.act("supply_accept", {"order_id": order_id})
        while True:
            order = next(row for row in accepted["data"]["overview"]["starter"]["supply"]["orders"]
                         if row["order_id"] == order_id)
            if order["status"] == "COMPLETED":
                break
            batch = next(row for row in order["delivery_batches"] if row["eligible_quantity"] > 0)
            accepted, delivery = self.act("supply_deliver", dict(order_id=order_id,
                instance_id=batch["instance_id"], quantity=batch["eligible_quantity"]), "GENERAL_STORE")
            self.assertEqual(accepted, execute_player_action(copy.deepcopy(delivery)))
        self.assertEqual(accepted["data"]["result"]["earned_gold"], 54)
        self.assertEqual(accepted["data"]["result"]["growth_rewards"][0]["xp_gained"], 20)
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 279)  # Initial funds + order + first gathering/supply goals.
        shop = accepted["data"]["overview"]["starter"]["gathering"]["tool_shop"]
        bought, purchase = self.act("buy_store", dict(listing_id=shop["listing_id"],
            quantity=1, expected_unit_price=shop["unit_price"]), "GENERAL_STORE")
        replayed_purchase = execute_player_action(copy.deepcopy(purchase))
        # Scoped store receipts retain their replay marker so clients refresh
        # historical shop projections. Every other field must remain identical.
        self.assertTrue(replayed_purchase.pop("replayed", False))
        self.assertEqual(bought, replayed_purchase)
        tool_id = bought["data"]["result"]["purchased_batches"][0]["instance_id"]
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 239)  # Tool costs 50; its one-time goal returns 10.
        for _ in range(5):
            latest, _ = self.act("collect", {"collect_type": "采集食材"}, "FOREST_EDGE")
        self.assertEqual(latest["data"]["overview"]["growth"]["skills"][0]["level"], 2)
        targeted, collection = self.act("collect", dict(collect_type="采集食材", category="leafy",
            tool_instance_id=tool_id), "FOREST_EDGE")
        self.assertEqual(targeted, execute_player_action(copy.deepcopy(collection)))
        self.assertTrue({row["item_id"] for row in targeted["data"]["result"]["collected_items"]} <= {1023, 1059})
        tool = InventorySlot.objects.get(bag=self.bag, instance_id=tool_id)
        self.assertEqual(tool.remaining_uses, 19)
        self.assertEqual(PlayerState.objects.get(player_id=str(self.player.pk)).action_points, 95)
        goals = {row["code"]: row["completed"] for row in targeted["data"]["overview"]["starter"]["goals"]}
        self.assertTrue(all(goals.values()), goals)
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 274)  # 200 + 54 - 50 + all five goals (70).
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="STARTER_GOAL_REWARD").count(), 5)
        self.assertTrue(all(row["reward_claimed"] for row in targeted["data"]["overview"]["starter"]["goals"]))
        self.assertTrue(targeted["data"]["overview"]["starter"]["supply"]["completed_personal"])

    def test_expired_order_maintenance_runs_from_world_time_without_a_player_click(self):
        from village.runtime_services import TimeService
        first, _ = self.act("collect", {"collect_type": "采集食材"}, "FOREST_EDGE")
        expiry = first["data"]["overview"]["starter"]["supply"]["orders"][0]["expires_world_minute"]
        fund = VillageAccount.objects.get(account_code="PUBLIC_FUND")
        self.assertEqual(fund.balance, 730)
        self.assertTrue(TimeService.receive_time(expiry, self.clock.time_revision + 1)[0])
        fund.refresh_from_db()
        self.assertEqual(fund.balance, 1000)
        self.assertTrue(reconcile(fund)["balance_matches"], reconcile(fund))
