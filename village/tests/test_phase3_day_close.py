"""Tests for Phase 3 day-close service.

Covers: day-close success, idempotency, sequential enforcement,
food spoilage, traveler sales, price calculation, restaurant/store status,
and rollback on error.
"""

import uuid

from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import (
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from recipes.models import Recipe, RecipeIngredient
from village.models import (
    DailyPriceSnapshot,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageRestaurantState,
    VillageSimulationConfig,
)
from village.phase3_contract import (
    DaySnapshotStatus,
    DeliveryStatus,
    OrderStatus,
    RestaurantStatus,
    ResultCode,
    StoreStatus,
)
from village.phase3_day_close import (
    calculate_price,
    get_restaurant_status,
    get_store_status,
    handle_day_close,
)


def _day_close_request_id(day_index: int) -> uuid.UUID:
    """Deterministic request_id for day-close."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"phase3:day-close:{day_index}")


class DayCloseTestBase(TestCase):
    """Shared setUp for day-close tests."""

    def setUp(self):
        VillageSimulationConfig.objects.create(
            config_key="default", village_economy_enabled=True,
        )
        self.time = GlobalGameTimeState.get_instance()
        # Day 1 = minutes 1440..2879; we are at minute 2000
        self.time.world_minute = 2000
        self.time.time_revision = 2000
        self.time.save()

        # NPCs
        self.bill, _ = NpcDefinition.objects.get_or_create(
            agent_id="10011", defaults={"name": "bill"},
        )
        self.jack, _ = NpcDefinition.objects.get_or_create(
            agent_id="10012", defaults={"name": "jack"},
        )
        self.mark, _ = NpcDefinition.objects.get_or_create(
            agent_id="10013", defaults={"name": "mark"},
        )
        self.lucy, _ = NpcDefinition.objects.get_or_create(
            agent_id="10014", defaults={"name": "lucy"},
        )
        self.nora, _ = NpcDefinition.objects.get_or_create(
            agent_id="10015", defaults={"name": "nora"},
        )

        # Inventories
        self.bill_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.bill, defaults={"money": 200},
        )
        self.jack_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.jack, defaults={"money": 240},
        )
        self.lucy_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.lucy, defaults={"money": 300},
        )
        self.nora_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.nora, defaults={"money": 450},
        )

        # PUBLIC_FUND
        self.account, _ = VillageAccount.objects.get_or_create(
            account_code="PUBLIC_FUND",
            defaults={"balance": 500, "last_closed_day_index": -1},
        )

        # Recipe setup
        self.recipe2, _ = Recipe.objects.update_or_create(
            pk=2, defaults={"item_id": 1053, "recipe_name": "辣子鸡丁"},
        )
        RecipeIngredient.objects.get_or_create(
            recipe=self.recipe2, item_id=1056,
            defaults={"item_name": "鸡肉", "count": 2},
        )
        self.recipe12, _ = Recipe.objects.update_or_create(
            pk=12, defaults={"item_id": 1067, "recipe_name": "奶酪焗玉米"},
        )
        RecipeIngredient.objects.get_or_create(
            recipe=self.recipe12, item_id=1030,
            defaults={"item_name": "玉米", "count": 1},
        )

        # Clean slots
        NpcGlobalInventorySlot.objects.filter(
            bag__in=[self.bill_inv, self.jack_inv, self.lucy_inv, self.nora_inv],
        ).delete()

    def _close_day(self, day_index: int = 0):
        """Execute day-close for the given day_index."""
        request_id = _day_close_request_id(day_index)
        return handle_day_close(
            {"day_index": day_index},
            self.time.world_minute,
            request_id,
        )


# ============================================================================
# Price calculation
# ============================================================================


class PriceCalculationTest(TestCase):
    def test_low_stock_125_percent(self):
        # round(12.5) = 12 (banker's rounding)
        self.assertEqual(calculate_price(10, 3, 10), 12)

    def test_normal_stock_base_price(self):
        self.assertEqual(calculate_price(10, 10, 10), 10)
        self.assertEqual(calculate_price(10, 15, 10), 10)
        self.assertEqual(calculate_price(10, 20, 10), 10)

    def test_surplus_stock_85_percent(self):
        # round(8.5) = 8 (banker's rounding)
        self.assertEqual(calculate_price(10, 21, 10), 8)
        self.assertEqual(calculate_price(10, 100, 10), 8)

    def test_minimum_price_1(self):
        self.assertEqual(calculate_price(1, 100, 10), 1)
        # round(1.25) = 1 (banker's rounding), max(1, 1) = 1
        self.assertEqual(calculate_price(1, 0, 10), 1)

    def test_zero_safe_stock(self):
        # safe_stock=0, available=0: 0 <= 0 (normal range) -> base_price
        self.assertEqual(calculate_price(10, 0, 0), 10)


# ============================================================================
# Restaurant status
# ============================================================================


class RestaurantStatusTest(TestCase):
    def test_sold_out_when_no_dishes(self):
        self.assertEqual(get_restaurant_status(0), RestaurantStatus.SOLD_OUT)

    def test_low_stock_when_less_than_max_sales(self):
        self.assertEqual(get_restaurant_status(1), RestaurantStatus.LOW_STOCK)

    def test_open_when_enough(self):
        self.assertEqual(get_restaurant_status(2), RestaurantStatus.OPEN)
        self.assertEqual(get_restaurant_status(10), RestaurantStatus.OPEN)


# ============================================================================
# Day-close success
# ============================================================================


class DayCloseSuccessTest(DayCloseTestBase):
    def test_day_close_success(self):
        """Day-close should succeed and update last_closed_day_index."""
        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertFalse(resp["replayed"])

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_closed_day_index, 0)

    def test_day_close_expires_orders(self):
        """OPEN/MATCHED orders past day_end_minute should be expired."""
        from inventory.models import NpcDefinition as NpcDef

        # Create an OPEN order that expires within day 0
        order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(),
            side="SELL",
            npc=self.bill,
            item_id=1030,
            amount=5,
            unit_price=8,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=500,  # within day 0 (0..1439)
            status=OrderStatus.OPEN,
        )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)

    def test_day_close_expires_deliveries(self):
        """ACTIVE deliveries past day_end_minute should be expired."""
        # Create a minimal order pair and delivery
        buy_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(),
            side="BUY",
            npc=self.jack,
            item_id=1030,
            amount=5,
            unit_price=8,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=500,
            status=OrderStatus.MATCHED,
        )
        sell_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(),
            side="SELL",
            npc=self.bill,
            item_id=1030,
            amount=5,
            unit_price=8,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=500,
            status=OrderStatus.MATCHED,
        )
        slot = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )
        delivery = NpcTradeDelivery.objects.create(
            buyer_order=buy_order,
            seller_order=sell_order,
            seller_slot_instance_id=slot.instance_id,
            reserved_amount=5,
            reserved_money=40,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=500,
            status=DeliveryStatus.ACTIVE,
        )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, DeliveryStatus.EXPIRED)

    def test_day_close_generates_price_draft(self):
        """Day-close should create a DRAFT price snapshot for next day."""
        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        snapshot = DailyPriceSnapshot.objects.get(day_index=1)
        self.assertEqual(snapshot.status, DaySnapshotStatus.DRAFT)
        self.assertIn("1030", snapshot.prices)

    def test_day_close_updates_restaurant_status(self):
        """Restaurant status should be updated after day-close."""
        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        rest_state = VillageRestaurantState.objects.get(day_index=0)
        self.assertIn(rest_state.status, [
            RestaurantStatus.OPEN,
            RestaurantStatus.LOW_STOCK,
            RestaurantStatus.SOLD_OUT,
        ])


# ============================================================================
# Day-close idempotency
# ============================================================================


class DayCloseIdempotentTest(DayCloseTestBase):
    def test_same_request_id_replays(self):
        """Same request_id should return replayed=True."""
        resp1 = self._close_day(0)
        self.assertEqual(resp1["result_code"], ResultCode.OK)
        self.assertFalse(resp1.get("replayed", False))

        resp2 = self._close_day(0)
        self.assertEqual(resp2["result_code"], ResultCode.OK)
        self.assertTrue(resp2["replayed"])

    def test_double_close_same_day_no_double_accounting(self):
        """Replayed day-close should not change account twice."""
        self._close_day(0)
        self.account.refresh_from_db()
        self.assertEqual(self.account.last_closed_day_index, 0)

        self._close_day(0)
        self.account.refresh_from_db()
        self.assertEqual(self.account.last_closed_day_index, 0)


# ============================================================================
# Day-close sequential
# ============================================================================


class DayCloseSequentialTest(DayCloseTestBase):
    def test_cannot_skip_days(self):
        """Cannot close day 1 before day 0."""
        resp = self._close_day(1)
        self.assertEqual(resp["result_code"], ResultCode.P3_DAY_NOT_READY)

    def test_must_close_in_order(self):
        """Must close days sequentially."""
        resp0 = self._close_day(0)
        self.assertEqual(resp0["result_code"], ResultCode.OK)

        # Now day 1 is the next expected day
        resp1 = self._close_day(1)
        self.assertEqual(resp1["result_code"], ResultCode.OK)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_closed_day_index, 1)

    def test_cannot_close_future_day(self):
        """Cannot close a day that hasn't happened yet."""
        # world_minute=2000 means current_day=1 (2000 // 1440 = 1)
        # Day 0 is valid, day 1 is current, day 2 is future
        self._close_day(0)
        self._close_day(1)
        resp = self._close_day(2)
        # Day 2 = minutes 2880..4319; current minute is 2000
        # current_day = 2000//1440 = 1, so day 2 > current_day
        self.assertEqual(resp["result_code"], ResultCode.P3_DAY_NOT_READY)


# ============================================================================
# Food spoilage
# ============================================================================


class FoodSpoilageTest(DayCloseTestBase):
    def test_expired_food_spoiled(self):
        """Food with expires_world_minute <= day_end should be deleted."""
        # Day 0 end = 1439. Food expires at 1000 (within day 0).
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1056, item_amount=5,
            produced_world_minute=500, expires_world_minute=1000,
        )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        slot = NpcGlobalInventorySlot.objects.filter(
            bag=self.lucy_inv, item_id=1056,
        ).first()
        # Slot should be deleted (amount was 0 after spoilage)
        self.assertIsNone(slot)

    def test_unexpired_food_preserved(self):
        """Food that expires after day_end should not be spoiled."""
        # Day 0 end = 1439. Food expires at 2000 (after day 0).
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1056, item_amount=5,
            produced_world_minute=500, expires_world_minute=2000,
        )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        slot = NpcGlobalInventorySlot.objects.filter(
            bag=self.lucy_inv, item_id=1056,
        ).first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.item_amount, 5)

    def test_reserved_items_not_spoiled(self):
        """Items reserved by ACTIVE delivery should not be spoiled."""
        slot = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
            produced_world_minute=500, expires_world_minute=1000,
        )

        # Create ACTIVE delivery reserving 3 items
        # Delivery expires AFTER day end so it stays ACTIVE during spoilage
        buy_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(), side="BUY", npc=self.jack,
            item_id=1030, amount=3, unit_price=8,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=5000, status=OrderStatus.MATCHED,
        )
        sell_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(), side="SELL", npc=self.bill,
            item_id=1030, amount=3, unit_price=8,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=5000, status=OrderStatus.MATCHED,
        )
        NpcTradeDelivery.objects.create(
            buyer_order=buy_order, seller_order=sell_order,
            seller_slot_instance_id=slot.instance_id,
            reserved_amount=3, reserved_money=24,
            delivery_point_code="MARKET_STREET",
            expires_world_minute=5000, status=DeliveryStatus.ACTIVE,
        )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        slot.refresh_from_db()
        # 10 total - 7 spoiled = 3 reserved remain
        self.assertEqual(slot.item_amount, 3)

    def test_spoilage_recorded_in_data(self):
        """Spoilage details should be in result data."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1056, item_amount=3,
            produced_world_minute=500, expires_world_minute=1000,
        )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        spoilage = resp["data"]["spoilage"]
        self.assertEqual(spoilage["count"], 3)
        self.assertEqual(len(spoilage["items_spoiled"]), 1)
        self.assertEqual(spoilage["items_spoiled"][0]["item_id"], 1056)


# ============================================================================
# Traveler sales
# ============================================================================


class TravelerSalesTest(DayCloseTestBase):
    def setUp(self):
        super().setUp()
        # Create published price snapshot
        DailyPriceSnapshot.objects.create(
            day_index=0,
            prices={"1053": 25, "1067": 22},
            status=DaySnapshotStatus.PUBLISHED,
        )

    def test_traveler_sales_success(self):
        """Lucy sells dishes to travelers at published price."""
        # Create 3 unexpired Lucy dishes
        for i in range(3):
            NpcGlobalInventorySlot.objects.create(
                bag=self.lucy_inv, slot_index=i, item_id=1053, item_amount=1,
                produced_world_minute=1000, expires_world_minute=4000,
                dish_quality=3, rated=False,
            )

        lucy_money_before = self.lucy_inv.money
        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        sales = resp["data"]["traveler_sales"]
        self.assertEqual(sales["sold"], 2)  # max 2 per day
        self.assertEqual(sales["revenue"], 50)  # 2 * 25

        self.lucy_inv.refresh_from_db()
        self.assertEqual(self.lucy_inv.money, lucy_money_before + 50)

    def test_traveler_sales_max_two_per_day(self):
        """At most 2 dishes sold per day-close."""
        for i in range(5):
            NpcGlobalInventorySlot.objects.create(
                bag=self.lucy_inv, slot_index=i, item_id=1053, item_amount=1,
                produced_world_minute=1000, expires_world_minute=4000,
            )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["traveler_sales"]["sold"], 2)

    def test_traveler_sales_fefo_order(self):
        """FEFO: earliest-expiring dishes sold first."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1053, item_amount=1,
            produced_world_minute=1000, expires_world_minute=5000,  # later
        )
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=1, item_id=1053, item_amount=1,
            produced_world_minute=800, expires_world_minute=3000,  # earlier
        )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        # The slot with expires=3000 should have been consumed first
        slots = list(NpcGlobalInventorySlot.objects.filter(
            bag=self.lucy_inv, item_id=1053,
        ).order_by("slot_index"))

        # slot 1 (expires=3000) should be gone or reduced
        self.assertEqual(slots[1].item_amount, 0)

    def test_no_traveler_sales_when_no_dishes(self):
        """No sales when Lucy has no dishes."""
        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["traveler_sales"]["sold"], 0)
        self.assertEqual(resp["data"]["traveler_sales"]["revenue"], 0)

    def test_traveler_sales_external_delta_positive(self):
        """Traveler sales should have positive external_delta."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1053, item_amount=2,
            produced_world_minute=1000, expires_world_minute=4000,
        )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertGreater(resp["external_delta"], 0)

    def test_traveler_sales_skips_expired_dishes(self):
        """Expired dishes should not be sold to travelers."""
        # Expired dish
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1053, item_amount=1,
            produced_world_minute=500, expires_world_minute=1000,  # expired by day end
        )
        # Valid dish
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=1, item_id=1053, item_amount=1,
            produced_world_minute=1000, expires_world_minute=4000,
        )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["traveler_sales"]["sold"], 1)

    def test_traveler_sales_skips_reserved_dishes(self):
        """Reserved dishes should not be sold to travelers."""
        slot = NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1053, item_amount=2,
            produced_world_minute=1000, expires_world_minute=4000,
        )

        # Reserve 2 via ACTIVE delivery
        buy_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(), side="BUY", npc=self.jack,
            item_id=1053, amount=2, unit_price=25,
            delivery_point_code="RESTAURANT",
            expires_world_minute=5000, status=OrderStatus.MATCHED,
        )
        sell_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(), side="SELL", npc=self.lucy,
            item_id=1053, amount=2, unit_price=25,
            delivery_point_code="RESTAURANT",
            expires_world_minute=5000, status=OrderStatus.MATCHED,
        )
        NpcTradeDelivery.objects.create(
            buyer_order=buy_order, seller_order=sell_order,
            seller_slot_instance_id=slot.instance_id,
            reserved_amount=2, reserved_money=50,
            delivery_point_code="RESTAURANT",
            expires_world_minute=5000, status=DeliveryStatus.ACTIVE,
        )

        resp = self._close_day(0)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["traveler_sales"]["sold"], 0)


# ============================================================================
# Day-close rollback on error
# ============================================================================


class DayCloseRollbackTest(DayCloseTestBase):
    def test_invalid_day_index_returns_error(self):
        """Invalid day_index should fail without side effects."""
        request_id = _day_close_request_id(999)
        resp = handle_day_close(
            {"day_index": -1},
            self.time.world_minute,
            request_id,
        )
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_closed_day_index, -1)
