"""Real row-lock contention, only against config.settings_postgres_test."""
import threading
import uuid

from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from inventory.models import NpcGlobalInventorySlot
from marketplace.models import MarketTransaction
from marketplace.services import create_system_listing
from player.models import PlayerState
from village.models import (VillageAffair,
                            VillageAffairContribution, VillageWorldEvent, VillagePlayerAffairLabor,
                            VillagePersonalCommission, VillagePersonalCommissionReceipt, NpcProfile)
from village.personal_commissions import publish_commission, CommissionError
from game_time.models import GlobalGameTimeState
from village.affairs import AffairError, ensure_bridge_affair, complete_labor
from village.player_affair_labor import tick_player_labor
from village.player_actions import execute_player_action
from village.tests.test_player_actions import PlayerFixture


class PostgreSQLPlayerActions(PlayerFixture, TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("requires isolated PostgreSQL settings")
        super().setUp()

    def race(self, *payloads):
        return self.race_calls(*(lambda p=p: execute_player_action(p) for p in payloads))

    def race_calls(self, *callbacks):
        barrier = threading.Barrier(len(callbacks))
        answers, errors = [], []

        def run(callback):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                answers.append(callback())
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(callback,), daemon=True) for callback in callbacks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
            self.assertFalse(t.is_alive(), "contender deadlock")
        self.assertEqual(errors, [], errors)
        self.assertEqual(len(answers), len(callbacks))
        return answers

    def commission_params(self):
        NpcProfile.objects.update_or_create(npc=self.npc, defaults=dict(display_name="Lucy", occupation="厨师",
            home_location_code="", work_location_code="", social_location_code="",
            capabilities=["PUBLISH_PERSONAL_COMMISSION"]))
        return dict(title="个人委托", reward_gold=5, valid_for_minutes=1440, default_location_code="",
                    stages=[dict(id="only", conditions=[dict(id="corn", type="item", item_ids=[1030], target=1)])])

    def test_two_players_cannot_accept_same_personal_commission(self):
        c = publish_commission(self.npc, self.commission_params(), uuid.uuid4(), self.clock.world_minute)
        answers = self.race(self.payload("commission_accept", dict(commission_id=c["commission_id"])),
                            self.payload("commission_accept", dict(commission_id=c["commission_id"]), player=self.b))
        self.assertEqual(sum(a["success"] for a in answers), 1)
        self.assertEqual(VillagePersonalCommission.objects.filter(status="ACTIVE").count(), 1)
        self.lucy.refresh_from_db(); self.assertEqual(self.lucy.money, 995)

    def test_two_players_buy_last_market_unit(self):
        listing, _ = create_system_listing(1030, 1, 10, snapshot_overrides=dict(
            producedWorldMinute=90, expiresWorldMinute=2000, batchMetadataVersion=1))
        params = dict(listing_id=str(listing.pk), quantity=1)
        answers = self.race(self.payload("buy_marketplace", params), self.payload("buy_marketplace", params, player=self.b))
        self.assertEqual(sum(a["success"] for a in answers), 1)
        self.assertEqual(MarketTransaction.objects.filter(listing=listing).count(), 1)
        self.bag_a.refresh_from_db(); self.bag_b.refresh_from_db()
        self.assertEqual(self.bag_a.money+self.bag_b.money, 190)

    def test_concurrent_commission_publications_cannot_overdraw_npc_budget(self):
        self.lucy.money = 5
        self.lucy.save()
        params = self.commission_params()
        def publish():
            try:
                publish_commission(self.npc, params, uuid.uuid4(), 100)
                return True
            except CommissionError as exc:
                self.assertEqual(exc.code, "INSUFFICIENT_GOLD")
                return False
        answers = self.race_calls(publish, publish)
        self.assertEqual(sum(answers), 1)
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 0)
        self.assertEqual(sum(c.reserved_reward_gold for c in VillagePersonalCommission.objects.all()), 5)

    def test_same_personal_completion_request_transfers_items_and_reward_once(self):
        c = publish_commission(self.npc, self.commission_params(), uuid.uuid4(), 100)
        self.assertTrue(self.run_action("commission_accept", dict(commission_id=c["commission_id"]))["success"])
        self.slot(1)
        p = self.payload("commission_contribute", dict(commission_id=c["commission_id"], condition_id="corn", quantity=1, item_id=1030))
        answers = self.race(p, p)
        self.assertTrue(all(a["success"] for a in answers), answers)
        self.assertEqual(answers[0], answers[1])
        self.bag_a.refresh_from_db(); self.lucy.refresh_from_db()
        self.assertEqual((self.bag_a.money, self.lucy.money), (105, 995))
        self.assertEqual(self.lucy.slots.get(item_id=1030).item_amount, 1)
        self.assertEqual(VillagePersonalCommissionReceipt.objects.filter(action="CONTRIBUTE").count(), 1)
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="PERSONAL_COMMISSION_COMPLETED").count(), 1)

    def test_two_players_contribute_last_required_material(self):
        from village.phase3_catalog import REPAIR_WOOD_ITEM_ID
        fact = self.project()
        with transaction.atomic():
            affair = ensure_bridge_affair(self.clock.world_minute)
            affair.progress["conditions"]["legacy_material_1002"] = 99
            affair.save()
        self.slot(1, item_id=REPAIR_WOOD_ITEM_ID)
        self.slot(1, item_id=REPAIR_WOOD_ITEM_ID, bag=self.bag_b)
        p = dict(affair_id="BRIDGE_REPAIR", kind="material", item_id=REPAIR_WOOD_ITEM_ID, quantity=1)
        answers = self.race(self.payload("contribute", p), self.payload("contribute", p, player=self.b))
        self.assertEqual(sum(a["success"] for a in answers), 1)
        fact.refresh_from_db()
        self.assertEqual(fact.repair_wood, 100)
        self.assertEqual(sum(s.item_amount for bag in (self.bag_a, self.bag_b) for s in bag.slots.all()), 1)
        self.assertEqual(VillageAffairContribution.objects.filter(affair=affair, kind="material").count(), 1)

    def test_two_players_contribute_last_required_gold(self):
        self.project()
        with transaction.atomic():
            affair = ensure_bridge_affair(self.clock.world_minute)
            affair.progress["conditions"]["legacy_gold"] = 499
            affair.save()
        params = dict(affair_id="BRIDGE_REPAIR", kind="fund", quantity=1)
        answers = self.race(self.payload("contribute", params), self.payload("contribute", params, player=self.b))
        self.assertEqual(sum(a["success"] for a in answers), 1)
        self.bag_a.refresh_from_db(); self.bag_b.refresh_from_db(); affair.refresh_from_db()
        self.assertEqual(self.bag_a.money+self.bag_b.money, 199)
        self.assertEqual(affair.progress["gold"], 500)
        self.assertEqual(VillageAffairContribution.objects.filter(affair=affair, kind="gold").count(), 1)

    def test_same_item_concurrent_contributions_stay_in_selected_conditions(self):
        from village.affairs import create_resource_affair
        with transaction.atomic():
            affair = create_resource_affair("TWO_TARGETS", "两份独立条件", dict(stages=[dict(
                id="collect", conditions=[dict(id="first", type="item", item_ids=[1030], target=1),
                                          dict(id="second", type="item", item_ids=[1030], target=1)])]), [])
        self.slot(1)
        self.slot(1, bag=self.bag_b)
        common = dict(affair_id=affair.pk, kind="item", item_id=1030, quantity=1)
        results = self.race(self.payload("contribute", dict(common, condition_id="first"), location=""),
                            self.payload("contribute", dict(common, condition_id="second"), player=self.b, location=""))
        self.assertTrue(all(result["success"] for result in results), results)
        affair.refresh_from_db()
        self.assertEqual(affair.progress["conditions"], {"first": 1, "second": 1})
        self.assertEqual(affair.status, "COMPLETED")
        self.assertEqual(sum(s.item_amount for bag in (self.bag_a, self.bag_b) for s in bag.slots.all()), 0)
        self.assertEqual(set(VillageAffairContribution.objects.filter(affair=affair).values_list("condition_id", flat=True)),
                         {"first", "second"})

    def pending_final_labor(self):
        from village.phase3_catalog import REPAIR_WOOD_ITEM_ID
        self.project()
        with transaction.atomic():
            affair = ensure_bridge_affair(self.clock.world_minute)
            affair.progress = dict(conditions={"legacy_material_1002": 100, "legacy_meals": 20,
                "legacy_gold": 500, "legacy_labor": 19},
                materials={str(REPAIR_WOOD_ITEM_ID): 100}, meals=20, gold=500, labor=19)
            affair.status = "ACTIVE"
            affair.save()
        nonce = str(uuid.uuid4())
        payload = self.payload("labor_start", dict(affair_id=affair.pk), location="OLD_BRIDGE")
        payload["authority_session_id"] = nonce
        result = execute_player_action(payload)
        self.assertTrue(result["success"], result)
        labor_id = result["data"]["result"]["labor_id"]
        tick = dict(player_id=str(self.a.pk), labor_id=labor_id, authority_session_id=nonce,
                    current_location_code="OLD_BRIDGE", present=True, observed_world_minute=self.clock.world_minute+30)
        self.clock.world_minute += 30; self.clock.time_revision += 1; self.clock.save()
        self.assertTrue(tick_player_labor(tick)["success"])
        self.clock.world_minute += 30; self.clock.time_revision += 1; self.clock.save()
        tick["observed_world_minute"] = self.clock.world_minute
        return affair, tick

    def assert_final_labor_once(self, affair):
        affair.refresh_from_db()
        self.assertEqual((affair.status, affair.progress["labor"]), ("COMPLETED", 20))
        self.assertEqual(VillageAffairContribution.objects.filter(affair=affair, kind="labor").count(), 1)
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="VILLAGE_AFFAIR_COMPLETED").count(), 1)

    def test_concurrent_same_labor_tick_is_settled_once(self):
        affair, tick = self.pending_final_labor()
        answers = self.race_calls(lambda: tick_player_labor(tick), lambda: tick_player_labor(tick))
        self.assertTrue(all(a["success"] for a in answers), answers)
        self.assertEqual(sum(a["data"]["credited_units"] for a in answers), 1)
        self.assertEqual(VillagePlayerAffairLabor.objects.get(pk=tick["labor_id"]).completed_units, 1)
        self.assert_final_labor_once(affair)

    def test_npc_and_player_race_for_last_labor_unit(self):
        affair, tick = self.pending_final_labor()

        def npc_completion():
            # NPC command completion uses this same world-first lock and shared
            # reducer after its trusted execution-duration check has passed.
            with transaction.atomic():
                clock = GlobalGameTimeState.objects.select_for_update().get(pk=1)
                row = VillageAffair.objects.select_for_update().get(pk=affair.pk)
                try:
                    complete_labor(row, world_minute=clock.world_minute, request_id=uuid.uuid4(), actor_npc=self.npc)
                    return 1
                except AffairError as exc:
                    self.assertEqual(exc.code, "AFFAIR_COMPLETED")
                    return 0

        answers = self.race_calls(lambda: tick_player_labor(tick)["data"]["credited_units"], npc_completion)
        self.assertEqual(sum(answers), 1)
        self.assert_final_labor_once(affair)

    def test_same_delivery_request_concurrently_pays_once(self):
        commission = self.commission(2)
        self.slot(2)
        payload = self.payload("commission_contribute", self.delivery_params(commission, 2))
        answers = self.race(payload, payload)
        self.assertTrue(all(a["success"] for a in answers), answers)
        self.assertEqual(answers[0], answers[1])
        self.bag_a.refresh_from_db()
        self.assertEqual(self.bag_a.money, 110)
        self.assertEqual(VillagePersonalCommissionReceipt.objects.filter(action="CONTRIBUTE").count(), 1)
        self.assertEqual(sum(s.item_amount for s in self.lucy.slots.all()), 2)

    def test_different_delivery_requests_cannot_double_consume(self):
        commission = self.commission(2)
        self.slot(2)
        params = self.delivery_params(commission, 2)
        answers = self.race(self.payload("commission_contribute", params), self.payload("commission_contribute", params))
        self.assertEqual(sum(a["success"] for a in answers), 1)
        self.assertEqual(VillagePersonalCommissionReceipt.objects.filter(action="CONTRIBUTE").count(), 1)

    def test_concurrent_collection_cannot_spend_same_action_points(self):
        from inventory.models import CollectionPoint, CollectionPointLevel, CollectionPointLevelItem
        point = CollectionPoint.objects.create(collect_type="采集食材", name="野外采集", item_type="蔬菜", action_cost=15, collect_count=1)
        level = CollectionPointLevel.objects.create(collection_point=point, level=1)
        CollectionPointLevelItem.objects.create(level=level, item_id=1030)
        PlayerState.objects.create(player_id=str(self.a.pk), action_points=15)
        params = dict(collect_type="采集食材")
        answers = self.race(self.payload("collect", params), self.payload("collect", params))
        self.assertEqual(sum(a["success"] for a in answers), 1)
        self.assertEqual(PlayerState.objects.get(player_id=str(self.a.pk)).action_points, 0)
