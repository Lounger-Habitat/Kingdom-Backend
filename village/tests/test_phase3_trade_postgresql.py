"""PostgreSQL concurrent tests for Phase 3 trade system.

Run only with ``config.settings_postgres_test``.  Each contender enters from
its own thread-local connection and waits at a barrier immediately before the
transactional service call.
"""

import threading
import uuid

from django.db import connection, connections
from django.test import TransactionTestCase

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from village.models import (
    NpcRuntimeState,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageSimulationConfig,
)
from village.phase3_asset_service import _ACTION_HANDLERS, commit_economy_action
from village.phase3_contract import VILLAGE_ECONOMY_ACTION_V1, ResultCode


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


class PostgreSQLPhaseThreeTradeConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("requires config.settings_postgres_test")

        _ensure_trade_handlers()

        VillageSimulationConfig.objects.create(
            config_key="default", village_economy_enabled=True,
        )
        self.time = GlobalGameTimeState.get_instance()
        self.time.world_minute = 700
        self.time.time_revision = 700
        self.time.save()

        self.bill, _ = NpcDefinition.objects.get_or_create(
            agent_id="10011", defaults={"name": "bill"},
        )
        self.jack, _ = NpcDefinition.objects.get_or_create(
            agent_id="10012", defaults={"name": "jack"},
        )

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

        for npc, loc in [(self.bill, "MARKET_STREET"), (self.jack, "MARKET_STREET")]:
            NpcRuntimeState.objects.get_or_create(
                npc=npc,
                defaults={"current_location_code": loc, "health_fp": 1000},
            )

        NpcGlobalInventorySlot.objects.filter(
            bag__in=[self.bill_inv, self.jack_inv],
        ).delete()

    # ── runner ───────────────────────────────────────────────────────────

    def _run_contenders(self, calls):
        barrier = threading.Barrier(len(calls), timeout=30)
        results = []
        failures = []
        lock = threading.Lock()

        def runner(label, call):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                outcome = call()
                with lock:
                    results.append((label, outcome))
            except BaseException as exc:
                with lock:
                    failures.append((label, exc))
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=runner, args=(l, c), daemon=True)
            for l, c in calls
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        alive = [t.name for t in threads if t.is_alive()]
        self.assertFalse(alive, f"Deadlock: threads still alive: {alive}")
        self.assertFalse(failures, f"Thread failures: {failures}")
        return dict(results)

    # ── concurrent delivery (same delivery pair) ─────────────────────────

    def test_concurrent_delivery_same_order_pair(self):
        """Two threads try START_DELIVERY on same delivery; only one succeeds."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )

        # Create matched orders
        sell_params = {
            "side": "SELL", "item_id": 1030, "amount": 5, "unit_price": 8,
            "delivery_point_code": "MARKET_STREET",
            "expires_world_minute": 2000,
            "expected_asset_version": self.bill_inv.asset_version,
        }
        sell_resp = commit_economy_action(
            _payload("CREATE_ORDER", 10011, "MARKET_STREET", sell_params)
        )
        self.assertEqual(sell_resp["result_code"], ResultCode.OK)

        self.jack_inv.refresh_from_db()
        buy_params = {
            "side": "BUY", "item_id": 1030, "amount": 5, "unit_price": 10,
            "delivery_point_code": "MARKET_STREET",
            "expires_world_minute": 2000,
            "expected_asset_version": self.jack_inv.asset_version,
        }
        buy_resp = commit_economy_action(
            _payload("CREATE_ORDER", 10012, "MARKET_STREET", buy_params)
        )
        self.assertEqual(buy_resp["result_code"], ResultCode.OK)

        delivery_id = buy_resp["effects"]["delivery"]["delivery_id"]

        # Both threads try to START_DELIVERY
        def make_delivery_call(rid):
            params = {
                "delivery_id": str(delivery_id),
                "buyer_npc_id": 10012,
                "seller_npc_id": 10011,
                "delivery_point_code": "MARKET_STREET",
            }
            return commit_economy_action(
                _payload("START_DELIVERY", 10011, "MARKET_STREET", params, rid)
            )

        rid1 = uuid.uuid4()
        rid2 = uuid.uuid4()
        outcomes = self._run_contenders([
            ("first", lambda: make_delivery_call(rid1)),
            ("second", lambda: make_delivery_call(rid2)),
        ])

        success_count = sum(
            1 for r in outcomes.values() if r.get("result_code") == ResultCode.OK
        )
        fail_count = sum(
            1 for r in outcomes.values()
            if r.get("result_code") == ResultCode.P3_DELIVERY_NOT_READY
        )
        self.assertEqual(success_count, 1, outcomes)
        self.assertEqual(fail_count, 1, outcomes)

        # Verify delivery is COMPLETED
        delivery = NpcTradeDelivery.objects.get(delivery_id=delivery_id)
        self.assertEqual(delivery.status, "COMPLETED")

        # No negative inventory
        for slot in NpcGlobalInventorySlot.objects.filter(bag=self.bill_inv):
            self.assertGreaterEqual(slot.item_amount, 0)

    # ── concurrent buy/sell no deadlock ───────────────────────────────────

    def test_concurrent_buy_sell_no_deadlock(self):
        """Two threads create opposite orders simultaneously; no deadlock."""
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=20,
        )

        def create_sell(rid):
            self.bill_inv.refresh_from_db()
            params = {
                "side": "SELL", "item_id": 1030, "amount": 5, "unit_price": 8,
                "delivery_point_code": "MARKET_STREET",
                "expires_world_minute": 2000,
                "expected_asset_version": self.bill_inv.asset_version,
            }
            return commit_economy_action(
                _payload("CREATE_ORDER", 10011, "MARKET_STREET", params, rid)
            )

        def create_buy(rid):
            self.jack_inv.refresh_from_db()
            params = {
                "side": "BUY", "item_id": 1030, "amount": 5, "unit_price": 10,
                "delivery_point_code": "MARKET_STREET",
                "expires_world_minute": 2000,
                "expected_asset_version": self.jack_inv.asset_version,
            }
            return commit_economy_action(
                _payload("CREATE_ORDER", 10012, "MARKET_STREET", params, rid)
            )

        rid_sell = uuid.uuid4()
        rid_buy = uuid.uuid4()
        outcomes = self._run_contenders([
            ("sell", lambda: create_sell(rid_sell)),
            ("buy", lambda: create_buy(rid_buy)),
        ])

        # Both should succeed (possibly matching)
        for label, result in outcomes.items():
            self.assertEqual(
                result["result_code"], ResultCode.OK,
                f"{label} failed: {result}",
            )

        # No negative inventory anywhere
        for slot in NpcGlobalInventorySlot.objects.filter(
            bag__in=[self.bill_inv, self.jack_inv]
        ):
            self.assertGreaterEqual(slot.item_amount, 0)

        # No negative money
        self.bill_inv.refresh_from_db()
        self.jack_inv.refresh_from_db()
        self.assertGreaterEqual(self.bill_inv.money, 0)
        self.assertGreaterEqual(self.jack_inv.money, 0)

        # Total money conserved
        total_money = self.bill_inv.money + self.jack_inv.money
        self.assertEqual(total_money, 200 + 240)
