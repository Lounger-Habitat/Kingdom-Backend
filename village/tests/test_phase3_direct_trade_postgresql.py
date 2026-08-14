"""PostgreSQL concurrent tests for Phase 3 direct trade.

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
    VillageAccount,
    VillageSimulationConfig,
    VillageStore,
    VillageStoreAccount,
    VillageStoreInventorySlot,
    VillageStoreListing,
    VillageTradeOffer,
)
from village.phase3_asset_service import commit_economy_action
from village.phase3_contract import VILLAGE_ECONOMY_ACTION_V1, ResultCode


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


class PostgreSQLPhaseThreeDirectTradeConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("requires config.settings_postgres_test")

        VillageSimulationConfig.objects.create(
            config_key="default",
            village_economy_enabled=True,
            village_direct_trade_enabled=True,
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
        self.nora, _ = NpcDefinition.objects.get_or_create(
            agent_id="10015", defaults={"name": "nora"},
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

        self.nora_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.nora, defaults={"money": 450},
        )
        self.nora_inv.money = 450
        self.nora_inv.save(update_fields=["money"])

        for npc, loc in [
            (self.bill, "GENERAL_STORE"),
            (self.jack, "GENERAL_STORE"),
            (self.nora, "GENERAL_STORE"),
        ]:
            NpcRuntimeState.objects.get_or_create(
                npc=npc,
                defaults={"current_location_code": loc, "health_fp": 1000},
            )

        self.public_fund, _ = VillageAccount.objects.get_or_create(
            account_code="PUBLIC_FUND", defaults={"balance": 500},
        )

        self.store, _ = VillageStore.objects.get_or_create(
            store_code="GENERAL_STORE",
            defaults={
                "location_code": "GENERAL_STORE",
                "operator_npc": self.nora,
                "display_name": "Nora 杂货铺",
                "status": "OPEN",
                "transaction_fee_bps": 500,
                "minimum_fee": 1,
                "inventory_version": 0,
            },
        )
        self.store_account, _ = VillageStoreAccount.objects.get_or_create(
            store=self.store, defaults={"balance": 0, "account_version": 0},
        )

        NpcGlobalInventorySlot.objects.filter(
            bag__in=[self.bill_inv, self.jack_inv, self.nora_inv],
        ).delete()
        VillageStoreInventorySlot.objects.filter(store=self.store).delete()
        VillageStoreListing.objects.filter(store=self.store).delete()
        VillageTradeOffer.objects.all().delete()

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

    # ── helpers ──────────────────────────────────────────────────────────

    def _list_corn(self, amount=10):
        NpcGlobalInventorySlot.objects.create(
            bag=self.nora_inv, slot_index=0, item_id=1030, item_amount=amount,
        )
        resp = commit_economy_action(_payload(
            "LIST_STORE_ITEM", 10015, "GENERAL_STORE",
            {"store_code": "GENERAL_STORE", "item_id": 1030, "amount": amount,
             "price_mode": "ORIGINAL"},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)
        return VillageStoreListing.objects.get(store=self.store, item_id=1030)

    # ── concurrent accept (same offer) ───────────────────────────────────

    def test_concurrent_accept_same_offer_only_once(self):
        NpcGlobalInventorySlot.objects.create(
            bag=self.jack_inv, slot_index=0, item_id=1030, item_amount=10,
        )
        NpcRuntimeState.objects.filter(npc=self.jack).update(
            current_location_code="MARKET_STREET"
        )
        NpcRuntimeState.objects.filter(npc=self.bill).update(
            current_location_code="MARKET_STREET"
        )

        prop = commit_economy_action(_payload(
            "PROPOSE_TRADE", 10011, "MARKET_STREET",
            {"target_npc_id": "10012", "item_id": 1030, "amount": 5,
             "gold_amount": 40, "expires_world_minute": 5000},
        ))
        self.assertEqual(prop["result_code"], ResultCode.OK)
        offer_id = prop["data"]["offer_id"]

        def make_accept(rid):
            return commit_economy_action(_payload(
                "RESPOND_TRADE", 10012, "MARKET_STREET",
                {"offer_id": str(offer_id), "decision": "ACCEPT"}, rid,
            ))

        outcomes = self._run_contenders([
            ("first", lambda: make_accept(uuid.uuid4())),
            ("second", lambda: make_accept(uuid.uuid4())),
        ])

        ok = [r for r in outcomes.values() if r.get("result_code") == ResultCode.OK]
        resolved = [
            r for r in outcomes.values()
            if r.get("result_code") == ResultCode.TRADE_OFFER_ALREADY_RESOLVED
        ]
        self.assertEqual(len(ok), 1, outcomes)
        self.assertEqual(len(resolved), 1, outcomes)

        offer = VillageTradeOffer.objects.get(offer_id=offer_id)
        self.assertEqual(offer.status, "ACCEPTED")
        # Jack's 10 corn reduced by exactly 5.
        jack_total = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.jack_inv, item_id=1030)
        )
        self.assertEqual(jack_total, 5)

    # ── concurrent buy (same listing) ────────────────────────────────────

    def test_concurrent_buy_same_listing_no_oversell(self):
        listing = self._list_corn(amount=10)  # unit_price = 8

        def make_buy(rid, npc_id):
            inv = NpcGlobalInventory.objects.get(npc__agent_id=str(npc_id))
            return commit_economy_action(_payload(
                "BUY_FROM_STORE", npc_id, "GENERAL_STORE",
                {"listing_id": str(listing.listing_id), "amount": 6,
                 "expected_buyer_asset_version": inv.asset_version}, rid,
            ))

        outcomes = self._run_contenders([
            ("bill", lambda: make_buy(uuid.uuid4(), 10011)),
            ("jack", lambda: make_buy(uuid.uuid4(), 10012)),
        ])

        ok = [r for r in outcomes.values() if r.get("result_code") == ResultCode.OK]
        insuff = [
            r for r in outcomes.values()
            if r.get("result_code") == ResultCode.TRADE_INSUFFICIENT_ITEM
        ]
        self.assertEqual(len(ok), 1, outcomes)
        self.assertEqual(len(insuff), 1, outcomes)

        # Exactly one 6-unit sale happened (10 -> 4 remaining).
        remaining = sum(
            s.item_amount for s in
            VillageStoreInventorySlot.objects.filter(store=self.store, item_id=1030)
        )
        self.assertEqual(remaining, 4)

        # gross = 8*6 = 48, fee = floor(48*500/10000)=2, net = 46.
        self.store_account.refresh_from_db()
        self.public_fund.refresh_from_db()
        self.assertEqual(self.store_account.balance, 46)
        self.assertEqual(self.public_fund.balance, 500 + 2)

        # No negative inventory and no double-accounting.
        for slot in VillageStoreInventorySlot.objects.filter(store=self.store):
            self.assertGreaterEqual(slot.item_amount, 0)
