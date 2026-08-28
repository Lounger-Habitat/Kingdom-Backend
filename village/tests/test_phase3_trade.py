"""Tests for Phase 3 trade actions: CREATE_ORDER, CANCEL_ORDER, START_DELIVERY.

Covers success paths, validation failures, auto-matching, reservation
correctness, and basic concurrency scenarios.
"""

import uuid

from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import (
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from village.models import (
    NpcRuntimeState,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageSimulationConfig,
)
from village.phase3_asset_service import (
    _ACTION_HANDLERS,
    commit_economy_action,
)
from village.phase3_contract import VILLAGE_ECONOMY_ACTION_V1, ResultCode


# ── Register trade handlers (normally done by _ensure_handlers) ──────────────


def _ensure_trade_handlers():
    if "CREATE_ORDER" not in _ACTION_HANDLERS:
        from village.phase3_trade import (
            _do_cancel_order,
            _do_create_order,
            _do_start_delivery,
        )

        _ACTION_HANDLERS.update(
            {
                "CREATE_ORDER": _do_create_order,
                "CANCEL_ORDER": _do_cancel_order,
                "START_DELIVERY": _do_start_delivery,
            }
        )


# ── Payload builder ──────────────────────────────────────────────────────────


def _payload(command_name, npc_id, location, params, request_id=None):
    return {
        "schema_version": VILLAGE_ECONOMY_ACTION_V1,
        "command_id": str(uuid.uuid4()),
        "request_id": str(request_id or uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "command_name": command_name,
        "npc_id": npc_id,
        "completed_world_minute": 700,
        "time_revision": 700,
        "current_location_code": location,
        "params": params,
    }


# ── Shared test base ─────────────────────────────────────────────────────────


class TradeTestBase(TestCase):
    """Shared setUp: config, time, NPCs, inventories, runtime states, items."""

    databases = "__all__"

    def setUp(self):
        _ensure_trade_handlers()

        VillageSimulationConfig.objects.create(
            config_key="default",
            village_economy_enabled=True,
        )
        self.time = GlobalGameTimeState.get_instance()
        self.time.world_minute = 700
        self.time.time_revision = 700
        self.time.save()

        # NPCs
        self.bill, _ = NpcDefinition.objects.get_or_create(
            agent_id="10011", defaults={"name": "bill"},
        )
        self.jack, _ = NpcDefinition.objects.get_or_create(
            agent_id="10012", defaults={"name": "jack"},
        )
        self.lucy, _ = NpcDefinition.objects.get_or_create(
            agent_id="10014", defaults={"name": "lucy"},
        )
        self.nora, _ = NpcDefinition.objects.get_or_create(
            agent_id="10015", defaults={"name": "nora"},
        )

        # Inventories (explicitly set money to handle migration-seeded records)
        self.bill_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.bill, defaults={"money": 200},
        )
        self.bill_inv.money = 200
        self.bill_inv.save(update_fields=["money"])

        self.jack_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.jack, defaults={"money": 240},
        )
        self.jack_inv.money = 240
        self.jack_inv.save(update_fields=["money"])

        self.lucy_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.lucy, defaults={"money": 300},
        )
        self.lucy_inv.money = 300
        self.lucy_inv.save(update_fields=["money"])

        self.nora_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.nora, defaults={"money": 450},
        )
        self.nora_inv.money = 450
        self.nora_inv.save(update_fields=["money"])

        # Runtime states (explicitly set location for migration-seeded records)
        for npc, loc in [
            (self.bill, "MARKET_STREET"),
            (self.jack, "MARKET_STREET"),
            (self.lucy, "MARKET_STREET"),
            (self.nora, "MARKET_STREET"),
        ]:
            rt, _ = NpcRuntimeState.objects.get_or_create(
                npc=npc,
                defaults={"current_location_code": loc, "health_fp": 1000},
            )
            rt.current_location_code = loc
            rt.health_fp = 1000
            rt.save(update_fields=["current_location_code", "health_fp"])

        # Clear existing slots for clean state
        NpcGlobalInventorySlot.objects.filter(
            bag__in=[self.bill_inv, self.jack_inv, self.lucy_inv, self.nora_inv],
        ).delete()

    # ── convenience builders ─────────────────────────────────────────────

    def _create_order(self, npc_id, location, side, item_id, amount, unit_price,
                      delivery_point="MARKET_STREET", expires=2000,
                      expected_version=None, request_id=None):
        if expected_version is None:
            inv = NpcGlobalInventory.objects.get(npc__agent_id=str(npc_id))
            expected_version = inv.asset_version
        params = {
            "side": side,
            "item_id": item_id,
            "amount": amount,
            "unit_price": unit_price,
            "delivery_point_code": delivery_point,
            "expires_world_minute": expires,
            "expected_asset_version": expected_version,
        }
        return commit_economy_action(
            _payload("CREATE_ORDER", npc_id, location, params, request_id)
        )

    def _cancel_order(self, npc_id, order_id, request_id=None):
        params = {"order_id": str(order_id)}
        return commit_economy_action(
            _payload("CANCEL_ORDER", npc_id, "MARKET_STREET", params, request_id)
        )

    def _start_delivery(self, seller_npc_id, delivery_id, buyer_npc_id,
                        delivery_point="MARKET_STREET", request_id=None):
        params = {
            "delivery_id": str(delivery_id),
            "buyer_npc_id": buyer_npc_id,
            "seller_npc_id": seller_npc_id,
            "delivery_point_code": delivery_point,
        }
        return commit_economy_action(
            _payload("START_DELIVERY", seller_npc_id, delivery_point, params,
                     request_id)
        )


# ============================================================================
# CREATE_ORDER success
# ============================================================================


class CreateOrderSellSuccessTest(TradeTestBase):
    def test_create_sell_order_success(self):
        """SELL order created with correct status and item reservation."""
        # Give Bill 10 corn
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )
        resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        order = NpcTradeOrder.objects.get(order_id=resp["effects"]["order"]["order_id"])
        self.assertEqual(order.status, "OPEN")
        self.assertEqual(order.side, "SELL")
        self.assertEqual(order.item_id, 1030)
        self.assertEqual(order.amount, 5)
        self.assertEqual(order.unit_price, 8)


class CreateOrderBuySuccessTest(TradeTestBase):
    def test_create_buy_order_success(self):
        """BUY order created when buyer has enough money."""
        resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        order = NpcTradeOrder.objects.get(order_id=resp["effects"]["order"]["order_id"])
        self.assertEqual(order.status, "OPEN")
        self.assertEqual(order.side, "BUY")


# ============================================================================
# CREATE_ORDER auto-match
# ============================================================================


class CreateOrderAutoMatchTest(TradeTestBase):
    def test_sell_then_buy_auto_match(self):
        """SELL then BUY with compatible price → both MATCHED, delivery ACTIVE."""
        # Bill has corn
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        # Bill sells 5 corn at price 8
        sell_resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        self.assertEqual(sell_resp["result_code"], ResultCode.OK)
        self.assertEqual(sell_resp["effects"]["order"]["status"], "OPEN")

        # Jack buys 5 corn at price 10 (>= 8, compatible)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        self.assertEqual(buy_resp["result_code"], ResultCode.OK)

        # Both should be MATCHED
        sell_order = NpcTradeOrder.objects.get(
            order_id=sell_resp["effects"]["order"]["order_id"]
        )
        buy_order = NpcTradeOrder.objects.get(
            order_id=buy_resp["effects"]["order"]["order_id"]
        )
        self.assertEqual(sell_order.status, "MATCHED")
        self.assertEqual(buy_order.status, "MATCHED")
        self.assertEqual(sell_order.matched_order_id, buy_order.order_id)
        self.assertEqual(buy_order.matched_order_id, sell_order.order_id)

        # Delivery created
        self.assertIn("delivery", buy_resp["effects"])
        delivery = NpcTradeDelivery.objects.get(
            delivery_id=buy_resp["effects"]["delivery"]["delivery_id"]
        )
        self.assertEqual(delivery.status, "ACTIVE")
        self.assertEqual(delivery.reserved_amount, 5)
        # Matched price = earlier order's price = SELL at 8
        self.assertEqual(delivery.reserved_money, 5 * 8)


class CreateOrderPriceIncompatibilityTest(TradeTestBase):
    def test_buy_price_below_sell_price_no_match(self):
        """BUY price < SELL price → both remain OPEN."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        # Bill sells at price 10
        sell_resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 10)
        self.assertEqual(sell_resp["result_code"], ResultCode.OK)

        # Jack buys at price 8 (< 10, incompatible)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 8)
        self.assertEqual(buy_resp["result_code"], ResultCode.OK)

        sell_order = NpcTradeOrder.objects.get(
            order_id=sell_resp["effects"]["order"]["order_id"]
        )
        buy_order = NpcTradeOrder.objects.get(
            order_id=buy_resp["effects"]["order"]["order_id"]
        )
        self.assertEqual(sell_order.status, "OPEN")
        self.assertEqual(buy_order.status, "OPEN")
        self.assertNotIn("delivery", buy_resp["effects"])


# ============================================================================
# CREATE_ORDER insufficient resources
# ============================================================================


class CreateOrderInsufficientItemTest(TradeTestBase):
    def test_sell_insufficient_items(self):
        """SELL with not enough available inventory → P3_INSUFFICIENT_ITEM."""
        # Bill only has 3 corn
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=3,
        )
        resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)


class CreateOrderInsufficientMoneyTest(TradeTestBase):
    def test_buy_insufficient_money(self):
        """BUY with not enough money → P3_INSUFFICIENT_MONEY."""
        # Jack has 240 money, trying to buy 5*100=500
        resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 100)
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_MONEY)


# ============================================================================
# CREATE_ORDER validation
# ============================================================================


class CreateOrderValidationTest(TradeTestBase):
    def test_invalid_side(self):
        resp = self._create_order(10012, "MARKET_STREET", "HOLD", 1030, 5, 8)
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_zero_amount(self):
        resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 0, 8)
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_zero_price(self):
        resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 0)
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_invalid_delivery_point(self):
        resp = self._create_order(10012, "NOWHERE", "BUY", 1030, 5, 8,
                                  delivery_point="NOWHERE")
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_expired_order(self):
        """expires_world_minute <= current world_minute → P3_INVALID_PARAMS."""
        resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 8,
                                  expires=700)
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)


# ============================================================================
# CANCEL_ORDER
# ============================================================================


class CancelOrderOpenTest(TradeTestBase):
    def test_cancel_open_order(self):
        """OPEN order cancelled successfully."""
        resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 8)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        order_id = resp["effects"]["order"]["order_id"]

        cancel_resp = self._cancel_order(10012, order_id)
        self.assertEqual(cancel_resp["result_code"], ResultCode.OK)

        order = NpcTradeOrder.objects.get(order_id=order_id)
        self.assertEqual(order.status, "CANCELLED")


class CancelOrderMatchedTest(TradeTestBase):
    def test_cancel_matched_order_releases_reservations(self):
        """MATCHED order cancellation → both orders CANCELLED, delivery CANCELLED."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        sell_resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        self.assertEqual(buy_resp["result_code"], ResultCode.OK)

        sell_order_id = sell_resp["effects"]["order"]["order_id"]
        buy_order_id = buy_resp["effects"]["order"]["order_id"]
        delivery_id = buy_resp["effects"]["delivery"]["delivery_id"]

        # Buyer cancels
        cancel_resp = self._cancel_order(10012, buy_order_id)
        self.assertEqual(cancel_resp["result_code"], ResultCode.OK)

        buy_order = NpcTradeOrder.objects.get(order_id=buy_order_id)
        sell_order = NpcTradeOrder.objects.get(order_id=sell_order_id)
        delivery = NpcTradeDelivery.objects.get(delivery_id=delivery_id)

        self.assertEqual(buy_order.status, "CANCELLED")
        self.assertEqual(sell_order.status, "CANCELLED")
        self.assertEqual(delivery.status, "CANCELLED")
        self.assertIsNone(buy_order.matched_order)
        self.assertIsNone(sell_order.matched_order)


class CancelOrderForbiddenActorTest(TradeTestBase):
    def test_cancel_wrong_npc(self):
        """Cancelling another NPC's order → P3_FORBIDDEN_ACTOR."""
        resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 8)
        order_id = resp["effects"]["order"]["order_id"]

        cancel_resp = self._cancel_order(10011, order_id)
        self.assertEqual(cancel_resp["result_code"], ResultCode.P3_FORBIDDEN_ACTOR)


class CancelOrderFilledTest(TradeTestBase):
    def test_cancel_filled_order_fails(self):
        """FILLED order cannot be cancelled → P3_ORDER_NOT_OPEN."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        sell_resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        self.assertEqual(buy_resp["result_code"], ResultCode.OK)

        sell_order_id = sell_resp["effects"]["order"]["order_id"]
        delivery_id = buy_resp["effects"]["delivery"]["delivery_id"]

        # Execute delivery
        delivery_resp = self._start_delivery(
            10011, delivery_id, 10012, "MARKET_STREET"
        )
        self.assertEqual(delivery_resp["result_code"], ResultCode.OK)

        # Try to cancel the filled order
        cancel_resp = self._cancel_order(10011, sell_order_id)
        self.assertEqual(cancel_resp["result_code"], ResultCode.P3_ORDER_NOT_OPEN)


# ============================================================================
# START_DELIVERY
# ============================================================================


class StartDeliverySuccessTest(TradeTestBase):
    def test_start_delivery_success(self):
        """Full delivery: items transferred, money transferred."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        sell_resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        delivery_id = buy_resp["effects"]["delivery"]["delivery_id"]

        # Record pre-delivery state
        bill_money_before = self.bill_inv.money
        jack_money_before = self.jack_inv.money

        resp = self._start_delivery(10011, delivery_id, 10012, "MARKET_STREET")
        self.assertEqual(resp["result_code"], ResultCode.OK)

        # Seller's items deducted
        slot = NpcGlobalInventorySlot.objects.get(
            bag=self.bill_inv, item_id=1030, slot_index=0
        )
        self.assertEqual(slot.item_amount, 5)

        # Buyer received items
        buyer_slots = NpcGlobalInventorySlot.objects.filter(
            bag=self.jack_inv, item_id=1030
        )
        total_received = sum(s.item_amount for s in buyer_slots)
        self.assertEqual(total_received, 5)

        # Money transferred: matched price = 8 (SELL order was earlier)
        self.bill_inv.refresh_from_db()
        self.jack_inv.refresh_from_db()
        self.assertEqual(self.bill_inv.money, bill_money_before + 5 * 8)
        self.assertEqual(self.jack_inv.money, jack_money_before - 5 * 8)

        # Statuses
        delivery = NpcTradeDelivery.objects.get(delivery_id=delivery_id)
        self.assertEqual(delivery.status, "COMPLETED")
        sell_order = NpcTradeOrder.objects.get(
            order_id=sell_resp["effects"]["order"]["order_id"]
        )
        buy_order = NpcTradeOrder.objects.get(
            order_id=buy_resp["effects"]["order"]["order_id"]
        )
        self.assertEqual(sell_order.status, "FILLED")
        self.assertEqual(buy_order.status, "FILLED")

    def test_start_delivery_only_requires_delivery_id(self):
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )
        self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)

        resp = commit_economy_action(_payload(
            "START_DELIVERY", 10011, "MARKET_STREET",
            {"delivery_id": buy_resp["effects"]["delivery"]["delivery_id"]},
        ))

        self.assertEqual(resp["result_code"], ResultCode.OK)


class StartDeliveryWrongLocationTest(TradeTestBase):
    def test_wrong_location_fails(self):
        """NPCs not at delivery point → P3_DELIVERY_NOT_READY."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        # Orders created at RESTAURANT delivery point (orders don't check location)
        sell_resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8,
                                       delivery_point="RESTAURANT")
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10,
                                      delivery_point="RESTAURANT")
        delivery_id = buy_resp["effects"]["delivery"]["delivery_id"]

        # NPCs are at MARKET_STREET, delivery is at RESTAURANT
        resp = self._start_delivery(10011, delivery_id, 10012, "RESTAURANT")
        self.assertEqual(resp["result_code"], ResultCode.P3_DELIVERY_NOT_READY)


class StartDeliveryForbiddenActorTest(TradeTestBase):
    def test_wrong_npc_as_seller(self):
        """Only the seller can initiate delivery -- buyer attempt fails."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        delivery_id = buy_resp["effects"]["delivery"]["delivery_id"]

        # Buyer (Jack, 10012) tries to start delivery as seller
        # npc_id=10012 but seller_npc_id=10011 → P3_FORBIDDEN_ACTOR
        params = {
            "delivery_id": str(delivery_id),
            "buyer_npc_id": 10012,
            "seller_npc_id": 10011,
            "delivery_point_code": "MARKET_STREET",
        }
        resp = commit_economy_action(
            _payload("START_DELIVERY", 10012, "MARKET_STREET", params)
        )
        self.assertEqual(resp["result_code"], ResultCode.P3_FORBIDDEN_ACTOR)


class StartDeliveryItemsExpiredTest(TradeTestBase):
    def test_expired_items_fail(self):
        """Items expired before delivery → P3_INSUFFICIENT_ITEM."""
        # Give Bill corn that expires at minute 750
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
            expires_world_minute=750,
        )

        sell_resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8,
                                       expires=2000)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10,
                                      expires=2000)
        delivery_id = buy_resp["effects"]["delivery"]["delivery_id"]

        # Advance time past expiry
        self.time.world_minute = 800
        self.time.save(update_fields=["world_minute"])

        resp = self._start_delivery(10011, delivery_id, 10012, "MARKET_STREET")
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)


class StartDeliveryAlreadyCompletedTest(TradeTestBase):
    def test_already_completed_fails(self):
        """Second delivery attempt on same delivery → P3_DELIVERY_NOT_READY."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        delivery_id = buy_resp["effects"]["delivery"]["delivery_id"]

        # First delivery succeeds
        resp1 = self._start_delivery(10011, delivery_id, 10012, "MARKET_STREET")
        self.assertEqual(resp1["result_code"], ResultCode.OK)

        # Second delivery fails
        resp2 = self._start_delivery(10011, delivery_id, 10012, "MARKET_STREET")
        self.assertEqual(resp2["result_code"], ResultCode.P3_DELIVERY_NOT_READY)


# ============================================================================
# Reservation correctness
# ============================================================================


class ReservedItemsNotAvailableForNewSellTest(TradeTestBase):
    def test_reserved_items_excluded_from_new_sell(self):
        """Items reserved by ACTIVE delivery cannot be sold again."""
        # Bill has 10 corn
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        # Bill sells 8 corn (matched)
        self._create_order(10011, "MARKET_STREET", "SELL", 1030, 8, 8)
        self._create_order(10012, "MARKET_STREET", "BUY", 1030, 8, 10)

        # Now Bill tries to sell 5 more (only 2 available)
        resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)

        # But selling 2 should work
        resp2 = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 2, 8)
        self.assertEqual(resp2["result_code"], ResultCode.OK)


class ReservedMoneyNotAvailableForNewBuyTest(TradeTestBase):
    def test_reserved_money_excluded_from_new_buy(self):
        """Money reserved by ACTIVE delivery cannot be used for another BUY."""
        # Give Bill items to sell
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=20,
        )

        # Jack has 240 money
        # First: Bill sells 5 corn at price 40 → matched, reserved_money = 200
        self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 40)
        self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 40)

        # Jack's available money = 240 - 200 = 40
        # Trying to buy 5 more at price 10 = 50 > 40
        resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_MONEY)

        # But buying 4 at price 10 = 40 should work
        resp2 = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 4, 10)
        self.assertEqual(resp2["result_code"], ResultCode.OK)


# ============================================================================
# Matching priority and price
# ============================================================================


class MatchingPriorityTest(TradeTestBase):
    def test_earliest_created_order_matched_first(self):
        """When multiple compatible orders exist, earliest created wins."""
        # Give items to Lucy and Bill
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1030, item_amount=10,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        # Lucy creates SELL first
        resp_lucy = self._create_order(10014, "MARKET_STREET", "SELL", 1030, 5, 8)
        # Bill creates SELL second
        resp_bill = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)

        # Jack creates BUY → should match with Lucy (earlier)
        resp_jack = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        self.assertEqual(resp_jack["result_code"], ResultCode.OK)

        lucy_order = NpcTradeOrder.objects.get(
            order_id=resp_lucy["effects"]["order"]["order_id"]
        )
        bill_order = NpcTradeOrder.objects.get(
            order_id=resp_bill["effects"]["order"]["order_id"]
        )
        jack_order = NpcTradeOrder.objects.get(
            order_id=resp_jack["effects"]["order"]["order_id"]
        )

        self.assertEqual(lucy_order.status, "MATCHED")
        self.assertEqual(bill_order.status, "OPEN")
        self.assertEqual(jack_order.status, "MATCHED")
        self.assertEqual(jack_order.matched_order_id, lucy_order.order_id)


class MatchingPriceTest(TradeTestBase):
    def test_matched_price_is_earlier_order_price(self):
        """Matched price = earlier order's unit_price."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        # Bill SELL at 6 (earlier)
        self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 6)
        # Jack BUY at 10 (later)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        self.assertEqual(buy_resp["result_code"], ResultCode.OK)

        delivery = NpcTradeDelivery.objects.get(
            delivery_id=buy_resp["effects"]["delivery"]["delivery_id"]
        )
        # Price = SELL's price (earlier) = 6
        self.assertEqual(delivery.reserved_money, 5 * 6)

    def test_matched_price_buyer_earlier(self):
        """When BUY order is earlier, matched price = BUY's price."""
        # Jack BUY at 10 (earlier)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)

        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )
        # Bill SELL at 6 (later)
        sell_resp = self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 6)
        self.assertEqual(sell_resp["result_code"], ResultCode.OK)

        # The SELL response should include the delivery
        self.assertIn("delivery", sell_resp["effects"])
        delivery = NpcTradeDelivery.objects.get(
            delivery_id=sell_resp["effects"]["delivery"]["delivery_id"]
        )
        # Price = BUY's price (earlier) = 10
        self.assertEqual(delivery.reserved_money, 5 * 10)


# ============================================================================
# Idempotent replay
# ============================================================================


class ReplayTest(TradeTestBase):
    def test_create_order_replay(self):
        """Same payload (same request_id + same hash) returns replayed result."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )
        params = {
            "side": "SELL",
            "item_id": 1030,
            "amount": 5,
            "unit_price": 8,
            "delivery_point_code": "MARKET_STREET",
            "expires_world_minute": 2000,
            "expected_asset_version": self.bill_inv.asset_version,
        }
        # Build payload ONCE so command_id/message_id/request_id are identical
        payload = _payload("CREATE_ORDER", 10011, "MARKET_STREET", params)
        resp1 = commit_economy_action(payload)
        self.assertEqual(resp1["result_code"], ResultCode.OK)
        self.assertFalse(resp1["replayed"])

        # Exact same payload → replayed
        resp2 = commit_economy_action(payload)
        self.assertEqual(resp2["result_code"], ResultCode.OK)
        self.assertTrue(resp2["replayed"])


# ============================================================================
# Internal trade conservation
# ============================================================================


class ConservationTest(TradeTestBase):
    def test_total_money_unchanged_after_delivery(self):
        """Internal trade: total NPC money unchanged after delivery."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        total_money_before = sum(
            inv.money for inv in
            NpcGlobalInventory.objects.filter(
                npc__agent_id__in=["10011", "10012", "10014", "10015"]
            )
        )

        self._create_order(10011, "MARKET_STREET", "SELL", 1030, 5, 8)
        buy_resp = self._create_order(10012, "MARKET_STREET", "BUY", 1030, 5, 10)
        delivery_id = buy_resp["effects"]["delivery"]["delivery_id"]

        resp = self._start_delivery(10011, delivery_id, 10012, "MARKET_STREET")
        self.assertEqual(resp["result_code"], ResultCode.OK)

        total_money_after = sum(
            inv.money for inv in
            NpcGlobalInventory.objects.filter(
                npc__agent_id__in=["10011", "10012", "10014", "10015"]
            )
        )
        self.assertEqual(total_money_before, total_money_after)
