"""Tests for Phase 3 economy models: constraints, creation, validation."""

import uuid

from django.db import IntegrityError, transaction
from django.test import TestCase

from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from village.models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageFarmPlot,
    VillageRestaurantState,
    VillageWorldEvent,
)


class VillageAccountTest(TestCase):
    """Test VillageAccount constraints."""

    def test_create_public_fund(self):
        acc = VillageAccount.objects.create(account_code="PUBLIC_FUND", balance=100)
        self.assertEqual(acc.balance, 100)
        self.assertEqual(acc.asset_version, 0)
        self.assertEqual(acc.last_closed_day_index, -1)

    def test_balance_non_negative_constraint(self):
        """VillageAccount.balance must be >= 0."""
        VillageAccount.objects.create(account_code="TEST_NEG", balance=0)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                acc = VillageAccount.objects.get(account_code="TEST_NEG")
                acc.balance = -1
                acc.save(update_fields=["balance"])

    def test_unique_account_code(self):
        VillageAccount.objects.create(account_code="UNIQUE_TEST", balance=0)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VillageAccount.objects.create(account_code="UNIQUE_TEST", balance=50)


class VillageFarmPlotTest(TestCase):
    """Test VillageFarmPlot creation and uniqueness."""

    def test_create_plots(self):
        for code in ("FARM_PLOT_A", "FARM_PLOT_B", "FARM_PLOT_C"):
            plot = VillageFarmPlot.objects.create(plot_code=code, status="EMPTY")
            self.assertEqual(plot.status, "EMPTY")
            self.assertIsNone(plot.batch_id)

    def test_unique_plot_code(self):
        VillageFarmPlot.objects.create(plot_code="PLOT_UNIQ", status="EMPTY")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VillageFarmPlot.objects.create(plot_code="PLOT_UNIQ", status="GROWING")

    def test_unique_batch_id(self):
        bid = uuid.uuid4()
        VillageFarmPlot.objects.create(
            plot_code="PLOT_B1", status="GROWING", batch_id=bid
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VillageFarmPlot.objects.create(
                    plot_code="PLOT_B2", status="GROWING", batch_id=bid
                )


class NpcTradeOrderTest(TestCase):
    """Test NpcTradeOrder constraints."""

    def setUp(self):
        self.npc = NpcDefinition.objects.create(agent_id="99001", name="test_npc")

    def test_create_buy_order(self):
        order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(),
            side="BUY",
            npc=self.npc,
            item_id=1030,
            amount=5,
            unit_price=10,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=1000,
        )
        self.assertEqual(order.status, "OPEN")
        self.assertEqual(order.amount, 5)

    def test_amount_must_be_positive(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NpcTradeOrder.objects.create(
                    request_id=uuid.uuid4(),
                    side="SELL",
                    npc=self.npc,
                    item_id=1030,
                    amount=0,
                    unit_price=10,
                    delivery_point_code="MARKET_STREET",
                    expires_world_minute=1000,
                )

    def test_unit_price_must_be_positive(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NpcTradeOrder.objects.create(
                    request_id=uuid.uuid4(),
                    side="SELL",
                    npc=self.npc,
                    item_id=1030,
                    amount=5,
                    unit_price=0,
                    delivery_point_code="MARKET_STREET",
                    expires_world_minute=1000,
                )

    def test_unique_request_id(self):
        rid = uuid.uuid4()
        NpcTradeOrder.objects.create(
            request_id=rid,
            side="BUY",
            npc=self.npc,
            item_id=1030,
            amount=5,
            unit_price=10,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=1000,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NpcTradeOrder.objects.create(
                    request_id=rid,
                    side="SELL",
                    npc=self.npc,
                    item_id=1030,
                    amount=3,
                    unit_price=8,
                    delivery_point_code="RESTAURANT",
                    expires_world_minute=500,
                )


class NpcTradeDeliveryTest(TestCase):
    """Test NpcTradeDelivery constraints."""

    def setUp(self):
        self.buyer = NpcDefinition.objects.create(agent_id="99002", name="buyer")
        self.seller = NpcDefinition.objects.create(agent_id="99003", name="seller")
        self.buy_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(),
            side="BUY",
            npc=self.buyer,
            item_id=1030,
            amount=5,
            unit_price=10,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=1000,
        )
        self.sell_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(),
            side="SELL",
            npc=self.seller,
            item_id=1030,
            amount=5,
            unit_price=8,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=1000,
        )

    def test_create_delivery(self):
        d = NpcTradeDelivery.objects.create(
            buyer_order=self.buy_order,
            seller_order=self.sell_order,
            seller_slot_instance_id=uuid.uuid4(),
            reserved_amount=5,
            reserved_money=50,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=1000,
        )
        self.assertEqual(d.status, "ACTIVE")

    def test_reserved_amount_non_negative(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NpcTradeDelivery.objects.create(
                    buyer_order=self.buy_order,
                    seller_order=self.sell_order,
                    seller_slot_instance_id=uuid.uuid4(),
                    reserved_amount=-1,
                    reserved_money=50,
                    delivery_point_code="MARKET_STREET",
                    expires_world_minute=1000,
                )

    def test_reserved_money_non_negative(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NpcTradeDelivery.objects.create(
                    buyer_order=self.buy_order,
                    seller_order=self.sell_order,
                    seller_slot_instance_id=uuid.uuid4(),
                    reserved_amount=5,
                    reserved_money=-1,
                    delivery_point_code="MARKET_STREET",
                    expires_world_minute=1000,
                )


class EconomicLedgerEntryTest(TestCase):
    """Test EconomicLedgerEntry creation."""

    def test_create_entry(self):
        entry = EconomicLedgerEntry.objects.create(
            request_id=uuid.uuid4(),
            entry_type="PRODUCTION",
            world_minute=500,
            changes={"10011": {"item_id": 1030, "delta": 5}},
            external_delta=0,
        )
        self.assertEqual(entry.entry_type, "PRODUCTION")
        self.assertEqual(entry.changes["10011"]["delta"], 5)

    def test_unique_request_id(self):
        rid = uuid.uuid4()
        EconomicLedgerEntry.objects.create(
            request_id=rid, entry_type="TRADE", world_minute=100
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                EconomicLedgerEntry.objects.create(
                    request_id=rid, entry_type="IMPORT", world_minute=200
                )


class DailyPriceSnapshotTest(TestCase):
    """Test DailyPriceSnapshot constraints."""

    def test_create_snapshot(self):
        snap = DailyPriceSnapshot.objects.create(
            day_index=0,
            prices={1030: 8, 1056: 12},
        )
        self.assertEqual(snap.status, "DRAFT")

    def test_unique_day_index(self):
        DailyPriceSnapshot.objects.create(day_index=5, prices={})
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DailyPriceSnapshot.objects.create(day_index=5, prices={})

    def test_day_index_non_negative(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DailyPriceSnapshot.objects.create(day_index=-1, prices={})


class VillageRestaurantStateTest(TestCase):
    """Test VillageRestaurantState constraints."""

    def test_create_state(self):
        state = VillageRestaurantState.objects.create(day_index=0)
        self.assertEqual(state.status, "OPEN")
        self.assertEqual(state.traveler_sales_count, 0)
        self.assertEqual(state.traveler_revenue, 0)

    def test_traveler_sales_non_negative(self):
        VillageRestaurantState.objects.create(day_index=10)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                s = VillageRestaurantState.objects.get(day_index=10)
                s.traveler_sales_count = -1
                s.save(update_fields=["traveler_sales_count"])

    def test_traveler_revenue_non_negative(self):
        VillageRestaurantState.objects.create(day_index=11)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                s = VillageRestaurantState.objects.get(day_index=11)
                s.traveler_revenue = -1
                s.save(update_fields=["traveler_revenue"])


class NpcGlobalInventorySlotPhase3FieldsTest(TestCase):
    """Test the 3 new fields on NpcGlobalInventorySlot."""

    def setUp(self):
        self.npc = NpcDefinition.objects.create(agent_id="99010", name="slot_test")
        self.bag = NpcGlobalInventory.objects.create(npc=self.npc, money=100)

    def test_create_slot_with_batch_fields(self):
        slot = NpcGlobalInventorySlot.objects.create(
            bag=self.bag,
            slot_index=0,
            item_id=1030,
            item_amount=5,
            produced_world_minute=500,
            expires_world_minute=3380,
            source_request_id=uuid.uuid4(),
        )
        slot.refresh_from_db()
        self.assertEqual(slot.produced_world_minute, 500)
        self.assertEqual(slot.expires_world_minute, 3380)
        self.assertIsNotNone(slot.source_request_id)

    def test_batch_fields_nullable(self):
        slot = NpcGlobalInventorySlot.objects.create(
            bag=self.bag,
            slot_index=1,
            item_id=1003,
            item_amount=10,
        )
        slot.refresh_from_db()
        self.assertIsNone(slot.produced_world_minute)
        self.assertIsNone(slot.expires_world_minute)
        self.assertIsNone(slot.source_request_id)
