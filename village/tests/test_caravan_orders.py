"""Small isolated settlement regressions; never use the saved village database."""
import uuid
from unittest.mock import patch

from django.test import TestCase

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from village import caravan, caravan_orders
from village.caravan_order_models import (VillageCaravanOrder, VillageCaravanOrderCompletion,
    VillageCaravanOrderDelivery, VillageCaravanOrderReceipt, VillageCaravanReward,
    VillageCaravanVisitAllocation)
from village.models import (EconomicLedgerEntry, VillageCaravanRequest, VillageCaravanVisit,
    VillageSimulationConfig, VillageStoryArc, VillageStoryFact, NpcProfile, NpcRuntimeState,
    NpcTradeOrder, NpcTradeDelivery)


class CaravanOrderTests(TestCase):
    def setUp(self):
        self.clock = GlobalGameTimeState.get_instance()
        self.set_minute(100)
        self.cfg, _ = VillageSimulationConfig.objects.update_or_create(config_key="default", defaults=dict(
            village_economy_enabled=True, caravan_orders_enabled=True, caravan_item_ids=[1049, 1051, 4001],
            caravan_export_item_ids=[1030, 1056], caravan_normal_capacity=60, caravan_export_capacity=60))
        arc, _ = VillageStoryArc.objects.get_or_create(arc_code="BROKEN_TRADE_ROUTE_V1")
        self.fact, _ = VillageStoryFact.objects.update_or_create(arc=arc, defaults=dict(bridge_condition="REPAIRED"))
        for item_id, kind, price in [(1030, "蔬菜", 8), (1056, "肉类", 12), (1049, "调料类", 2),
                                     (1051, "种子", 3), (4001, "工具", 50)]:
            Item.objects.update_or_create(item_id=item_id, defaults=dict(item_name=str(item_id), item_type=kind,
                item_price=price, is_deleted=False))
        self.player = GameAccount.objects.create(username="order-seller")
        self.other = GameAccount.objects.create(username="order-other")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=20)
        self.other_bag = InventoryBag.objects.create(player=self.other, character_id=self.other.username, money=30)
        self.corn = InventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=1030, item_amount=50, quality=4)
        self.meat = InventorySlot.objects.create(bag=self.bag, slot_index=1, item_id=1056, item_amount=50, quality=3)

    def set_minute(self, minute):
        self.clock.world_minute, self.clock.time_revision = minute, minute
        self.clock.save()

    def publish_and_arrive(self):
        order = caravan_orders.ensure_orders(self.clock.world_minute)
        self.assertIsNotNone(order)
        self.set_minute(1440 + caravan.ARRIVAL)
        visit = caravan.sync_visit(self.clock.world_minute)
        return order, visit

    def params(self, order, slot, quantity):
        return dict(order_id=str(order.pk), item_id=slot.item_id, instance_id=str(slot.instance_id), quantity=quantity)

    def deliver(self, params, rid=None, actor=None, location="TRADE_ROAD"):
        return caravan_orders.deliver_order("PLAYER", str((actor or self.player).pk), params,
            self.clock.world_minute, rid or uuid.uuid4(), location)

    def finish(self, order):
        for line in order.lines.all():
            slot = self.corn if line.item_id == 1030 else self.meat
            answer = self.deliver(self.params(order, slot, line.target_quantity - line.delivered_quantity))
            self.assertEqual(answer["result_code"], "OK", answer)

    def test_partial_payment_quality_preview_ownership_and_reserved_transport(self):
        # A query cannot publish an order or generate a visit.
        caravan_orders.orders_overview("PLAYER", self.player.pk, 100)
        self.assertFalse(VillageCaravanOrder.objects.exists())
        order, visit = self.publish_and_arrive()
        view = caravan_orders.orders_overview("PLAYER", self.player.pk, self.clock.world_minute)
        line = next(row for row in view["active_order"]["lines"] if row["item_id"] == 1030)
        unit = line["quality_prices"]["4"]
        params = self.params(order, self.corn, 3)
        shared_rid = uuid.uuid4()
        self.assertEqual(self.deliver(params, rid=shared_rid, actor=self.other)["result_code"], "CARAVAN_ORDER_ITEM_NOT_OWNED")
        self.assertEqual(self.deliver(params, location="GENERAL_STORE")["result_code"], "LOCATION_INVALID")
        answer = self.deliver(dict(params, expected_unit_price=unit), rid=shared_rid)
        self.assertEqual(answer["result_code"], "OK", answer)
        self.assertEqual(answer["data"]["total_income"], 3 * unit)
        self.bag.refresh_from_db()
        self.corn.refresh_from_db()
        self.assertEqual((self.bag.money, self.corn.item_amount), (20 + 3 * unit, 47))
        self.assertFalse(VillageCaravanReward.objects.exists())
        visit.refresh_from_db()
        allocation = VillageCaravanVisitAllocation.objects.get(visit=visit)
        self.assertEqual(visit.exported_amount, 3)
        self.assertEqual(caravan_orders.export_allowance(visit, 1030), 60 - allocation.order_reserved_capacity)
        self.assertEqual(EconomicLedgerEntry.objects.get(request_id=answer["request_id"]).external_delta, 3 * unit)
        self.assertFalse(caravan_orders.orders_overview("PUBLIC", "", self.clock.world_minute)["active_order"]["lines"][0]["deliverable_batches"])
        # The NPC entry uses its real bag, backend location/body and existing
        # delivery reservation, without Nora or ordinary export authority.
        npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults=dict(name="Bill"))
        NpcProfile.objects.update_or_create(npc=npc, defaults=dict(display_name="Bill", occupation="farmer",
            home_location_code="BILL_HOME", work_location_code="BILL_FARM", social_location_code="VILLAGE_SQUARE",
            capabilities=["DELIVER_CARAVAN_ORDER"]))
        rt, _ = NpcRuntimeState.objects.update_or_create(npc=npc, defaults=dict(current_location_code="TRADE_ROAD",
            action_status="IDLE", health_fp=1000))
        npc_bag, _ = NpcGlobalInventory.objects.update_or_create(npc=npc, defaults=dict(money=10))
        source = NpcGlobalInventorySlot.objects.create(bag=npc_bag, slot_index=0, item_id=1056, item_amount=3, quality=3)
        old = NpcTradeOrder.objects.create(request_id=uuid.uuid4(), npc=npc, side="SELL", item_id=1056,
            amount=2, unit_price=1, delivery_point_code="TRADE_ROAD", expires_world_minute=3000)
        held = NpcTradeDelivery.objects.create(buyer_order=old, seller_order=old,
            seller_slot_instance_id=source.instance_id, reserved_amount=2, reserved_money=0,
            delivery_point_code="TRADE_ROAD", expires_world_minute=3000)
        def npc_deliver(quantity):
            return caravan_orders.deliver_order("NPC", "10011", self.params(order, source, quantity),
                self.clock.world_minute, uuid.uuid4(), "TRADE_ROAD")
        self.assertEqual(npc_deliver(2)["result_code"], "INSUFFICIENT_QUALIFIED_ITEMS")
        rt.health_fp = 100
        rt.save(update_fields=["health_fp"])
        self.assertEqual(npc_deliver(1)["result_code"], "HEALTH_TOO_LOW")
        rt.health_fp = 1000
        rt.save(update_fields=["health_fp"])
        paid = npc_deliver(1)
        self.assertEqual(paid["result_code"], "OK", paid)
        source.refresh_from_db()
        held.refresh_from_db()
        self.assertEqual((source.item_amount, held.reserved_amount), (2, 2))

    def test_partial_expiry_preserves_trade_and_disabling_does_not_erase_terms(self):
        order, _ = self.publish_and_arrive()
        first = self.deliver(self.params(order, self.corn, 2))
        self.cfg.caravan_orders_enabled = False
        self.cfg.save(update_fields=["caravan_orders_enabled"])
        self.assertEqual(self.deliver(self.params(order, self.corn, 1))["result_code"], "OK")
        self.bag.refresh_from_db()
        money = self.bag.money
        self.set_minute(order.deadline_world_minute)
        self.assertEqual(caravan_orders.expire_orders(0), 1)  # authoritative clock, not caller time
        self.assertEqual(self.deliver(self.params(order, self.corn, 1))["result_code"], "CARAVAN_ORDER_EXPIRED")
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, money)
        self.assertEqual(VillageCaravanOrderDelivery.objects.count(), 2)
        self.assertEqual(VillageCaravanOrderCompletion.objects.count(), 0)
        self.assertEqual(EconomicLedgerEntry.objects.get(request_id=first["request_id"]).external_delta, first["data"]["total_income"])

    def test_replay_conflict_atomic_completion_and_stale_last_quantity(self):
        order, _ = self.publish_and_arrive()
        params, rid = self.params(order, self.corn, 2), uuid.uuid4()
        first = self.deliver(params, rid)
        self.assertTrue(self.deliver(params, rid)["replayed"])
        self.assertEqual(self.deliver(dict(params, quantity=1), rid)["result_code"], "REQUEST_CONFLICT")
        self.assertEqual(VillageCaravanOrderDelivery.objects.count(), 1)
        # A failure after all money/item mutations rolls back the complete write set.
        before_money = InventoryBag.objects.get(pk=self.bag.pk).money
        with patch("village.caravan_orders.EconomicLedgerEntry.objects.create", side_effect=RuntimeError("ledger unavailable")):
            with self.assertRaises(RuntimeError):
                self.deliver(self.params(order, self.meat, 1))
        self.assertEqual(InventoryBag.objects.get(pk=self.bag.pk).money, before_money)
        self.assertEqual(InventorySlot.objects.get(pk=self.meat.pk).item_amount, 50)
        for line in order.lines.all():
            slot = self.corn if line.item_id == 1030 else self.meat
            remaining = line.target_quantity - line.delivered_quantity
            if slot == self.meat:
                remaining -= 1
            if remaining:
                self.assertEqual(self.deliver(self.params(order, slot, remaining))["result_code"], "OK")
        stale = self.params(order, self.meat, 2)
        self.assertEqual(self.deliver(stale)["result_code"], "CARAVAN_ORDER_REMAINING_CHANGED")
        last, last_id = self.params(order, self.meat, 1), uuid.uuid4()
        answer = self.deliver(last, last_id)
        self.assertTrue(answer["data"]["completed"])
        self.assertTrue(self.deliver(last, last_id)["replayed"])
        self.assertEqual(self.deliver(last)["result_code"], "CARAVAN_ORDER_CLOSED")
        self.assertEqual(VillageCaravanOrderCompletion.objects.count(), 1)
        self.assertEqual(VillageCaravanReward.objects.count(), 1)
        self.assertEqual(EconomicLedgerEntry.objects.filter(request_id=last_id).count(), 1)
        self.assertIsNone(caravan_orders.ensure_orders(self.clock.world_minute))

    def test_next_real_normal_visit_reward_once_kind_capacity_and_no_compounding(self):
        order, current = self.publish_and_arrive()
        order.rewards = dict(buy_capacity=dict(item_ids=[1030], bonus_capacity=15),
            import_capacity=dict(bonus_capacity=15), extra_kinds=[dict(item_id=1051, minimum_quantity=3),
            dict(item_id=4001, minimum_quantity=3)], max_import_bonus=15)
        order.save(update_fields=["rewards"])
        self.finish(order)
        caravan_orders.apply_visit_rewards(current)
        current.refresh_from_db()
        self.assertEqual(current.export_capacity, 60)
        self.fact.bridge_condition, self.fact.emergency_caravan_succeeded = "BROKEN", True
        self.fact.save()
        self.set_minute(2 * 1440 + caravan.ARRIVAL)
        self.assertEqual(caravan.sync_visit(self.clock.world_minute).route, "EMERGENCY")
        self.assertEqual(VillageCaravanReward.objects.get().status, "PENDING")
        self.fact.bridge_condition = "REPAIRED"
        self.fact.save()
        VillageCaravanRequest.objects.create(day_index=3, item_ids=[1049], submitted_world_minute=self.clock.world_minute)
        self.set_minute(3 * 1440 + caravan.ARRIVAL)
        visit = caravan.sync_visit(self.clock.world_minute)
        self.assertEqual(sum(visit.initial_stock.values()), 75)
        self.assertGreaterEqual(visit.initial_stock["1051"], 3)
        self.assertGreaterEqual(visit.initial_stock["4001"], 3)
        self.assertEqual(visit.export_capacity, 75)
        self.assertEqual(caravan_orders.export_allowance(visit, 1030), 75)
        self.assertEqual(caravan_orders.export_allowance(visit, 1056), 60)
        caravan_orders.record_ordinary_export(visit, 1030, 15)
        self.assertEqual(caravan_orders.export_allowance(visit, 1030), 60)
        self.assertEqual(caravan_orders.export_allowance(visit, 1056), 60)
        caravan_orders.record_ordinary_export(visit, 1056, 60)
        self.assertEqual(caravan_orders.export_allowance(visit, 1030), 0)
        self.assertEqual(caravan_orders.export_allowance(visit, 1056), 0)
        reward = VillageCaravanReward.objects.get()
        self.assertEqual((reward.status, reward.visit_id), ("APPLIED", 3))
        caravan_orders.apply_visit_rewards(visit)
        self.set_minute(4 * 1440 + caravan.ARRIVAL)
        following = caravan.sync_visit(self.clock.world_minute)
        self.assertEqual(following.export_capacity, 60)
        self.assertEqual(sum(following.initial_stock.values()), 60)
