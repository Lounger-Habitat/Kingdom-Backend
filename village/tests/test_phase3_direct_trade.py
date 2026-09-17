"""Tests for Phase 3 direct trade & independent store commands.

Covers the design 14.1 must-test items: store/operator asset isolation,
operator-only permissions, original vs custom price, batch preservation,
delist return, public inventory filtering, backend-computed pricing and fee
split, fee/public-fund failure rollback, operator self-buy prohibition, offer
response/cancel authorisation, colocation, idempotency, version bumps,
PUBLIC_FUND asset version, audit reconciliation, ledger/event consistency, and
legacy order migration.
"""

import uuid

from django.db.models import Max
from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import (
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from items.models import Item
from village.models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    NpcRuntimeState,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageActionReceipt,
    VillageSimulationConfig,
    VillageStoryArc,
    VillageStoryFact,
    VillageStore,
    VillageStoreAccount,
    VillageStoreInventorySlot,
    VillageStoreListing,
    VillageTradeOffer,
    VillageWorldEvent,
)
from village.phase3_asset_service import commit_economy_action
from village.phase3_contract import (
    VILLAGE_ECONOMY_ACTION_V1,
    DaySnapshotStatus,
    ListingStatus,
    PriceMode,
    ResultCode,
    TradeOfferStatus,
)
from village.phase3_direct_trade import (
    _compute_fee,
    migrate_legacy_trade_orders,
    query_store_inventory,
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


class DirectTradeTestBase(TestCase):
    """Shared setup: config, time, NPCs, inventories, stores, PUBLIC_FUND."""

    def setUp(self):
        VillageSimulationConfig.objects.get_or_create(config_key="default")
        cfg = VillageSimulationConfig.objects.get(config_key="default")
        cfg.village_economy_enabled = True
        cfg.village_direct_trade_enabled = True
        cfg.save(update_fields=["village_economy_enabled", "village_direct_trade_enabled"])

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
        self.mark, _ = NpcDefinition.objects.get_or_create(
            agent_id="10013", defaults={"name": "mark"},
        )
        self.lucy, _ = NpcDefinition.objects.get_or_create(
            agent_id="10014", defaults={"name": "lucy"},
        )
        self.nora, _ = NpcDefinition.objects.get_or_create(
            agent_id="10015", defaults={"name": "nora"},
        )

        NpcGlobalInventory.objects.get_or_create(npc=self.mark, defaults={"money":0})
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

        for npc in (self.bill, self.jack, self.mark, self.lucy, self.nora):
            rt, _ = NpcRuntimeState.objects.get_or_create(
                npc=npc,
                defaults={"current_location_code": "MARKET_STREET", "health_fp": 1000},
            )
            rt.current_location_code = "MARKET_STREET"
            rt.health_fp = 1000
            rt.save(update_fields=["current_location_code", "health_fp"])

        self.public_fund, _ = VillageAccount.objects.get_or_create(
            account_code="PUBLIC_FUND", defaults={"balance": 500, "last_closed_day_index": -1},
        )
        self.public_fund.balance = 500
        self.public_fund.audit_opening_balance = 500
        self.public_fund.asset_version = 0
        self.public_fund.save(update_fields=["balance", "audit_opening_balance", "asset_version"])

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
        self.store.operator_npc = self.nora
        self.store.location_code = "GENERAL_STORE"
        self.store.status = "OPEN"
        self.store.transaction_fee_bps = 500
        self.store.minimum_fee = 1
        self.store.inventory_version = 0
        self.store.save(update_fields=[
            "operator_npc", "location_code", "status",
            "transaction_fee_bps", "minimum_fee", "inventory_version",
        ])

        self.store_account, _ = VillageStoreAccount.objects.get_or_create(
            store=self.store, defaults={"balance": 0, "account_version": 0},
        )
        self.store_account.balance = 0
        self.store_account.account_version = 0
        self.store_account.save(update_fields=["balance", "account_version"])

        for item_id, item_name, item_price in (
            (1030, "玉米", 8),
            (1056, "鸡肉", 12),
            (1049, "盐", 2),
            (4001, "阶段三农具", 50),
        ):
            Item.objects.update_or_create(
                item_id=item_id,
                defaults={
                    "item_name": item_name,
                    "item_price": item_price,
                    "is_deleted": False,
                },
            )

        self.rest_store, _ = VillageStore.objects.get_or_create(
            store_code="RESTAURANT_STORE",
            defaults={
                "location_code": "RESTAURANT",
                "operator_npc": self.lucy,
                "display_name": "Lucy 饭店",
                "status": "OPEN",
                "transaction_fee_bps": 500,
                "minimum_fee": 1,
                "inventory_version": 0,
            },
        )

        NpcGlobalInventorySlot.objects.filter(
            bag__in=[self.bill_inv, self.jack_inv, self.lucy_inv, self.nora_inv],
        ).delete()
        VillageStoreInventorySlot.objects.filter(
            store__in=[self.store, self.rest_store],
        ).delete()
        VillageStoreListing.objects.filter(
            store__in=[self.store, self.rest_store],
        ).delete()
        VillageTradeOffer.objects.all().delete()

    # ── helpers ────────────────────────────────────────────────────────────

    def _set_location(self, npc_id, loc):
        rt = NpcRuntimeState.objects.get(npc__agent_id=str(npc_id))
        rt.current_location_code = loc
        rt.save(update_fields=["current_location_code"])

    def _give_item(self, inv, item_id, amount, produced=None, expires=None):
        max_idx = (
            NpcGlobalInventorySlot.objects.filter(bag=inv)
            .aggregate(m=Max("slot_index"))["m"]
        )
        next_idx = 0 if max_idx is None else max_idx + 1
        NpcGlobalInventorySlot.objects.create(
            bag=inv, slot_index=next_idx, item_id=item_id, item_amount=amount,
            produced_world_minute=produced, expires_world_minute=expires,
        )

    def _list(self, npc_id, store_code, item_id, amount, price_mode,
              custom=None, exp_op=None, exp_store=None, location="GENERAL_STORE",
              request_id=None):
        params = {
            "store_code": store_code, "item_id": item_id, "amount": amount,
            "price_mode": price_mode,
        }
        if custom is not None:
            params["custom_unit_price"] = custom
        if exp_op is not None:
            params["expected_operator_asset_version"] = exp_op
        if exp_store is not None:
            params["expected_store_inventory_version"] = exp_store
        return commit_economy_action(
            _payload("LIST_STORE_ITEM", npc_id, location, params, request_id)
        )

    def _buy(self, npc_id, listing_id, amount, exp_store=None, exp_buyer=None,
             location="GENERAL_STORE", request_id=None):
        params = {"listing_id": str(listing_id), "amount": amount}
        if exp_store is not None:
            params["expected_store_inventory_version"] = exp_store
        if exp_buyer is not None:
            params["expected_buyer_asset_version"] = exp_buyer
        return commit_economy_action(
            _payload("BUY_FROM_STORE", npc_id, location, params, request_id)
        )

    def _propose(self, buyer_id, target_id, item_id, amount, gold, location="MARKET_STREET",
                 expires=2000, request_id=None, side=None):
        params = {
            "target_npc_id": str(target_id), "item_id": item_id, "amount": amount,
            "gold_amount": gold, "expires_world_minute": expires,
        }
        if side is not None:
            params["side"] = side
        return commit_economy_action(
            _payload("PROPOSE_TRADE", buyer_id, location, params, request_id)
        )

    def _respond(self, seller_id, offer_id, decision, location="MARKET_STREET",
                 request_id=None):
        params = {"offer_id": str(offer_id), "decision": decision}
        return commit_economy_action(
            _payload("RESPOND_TRADE", seller_id, location, params, request_id)
        )

    def _cancel_offer(self, buyer_id, offer_id, location="MARKET_STREET", request_id=None):
        params = {"offer_id": str(offer_id)}
        return commit_economy_action(
            _payload("CANCEL_TRADE_OFFER", buyer_id, location, params, request_id)
        )


# ============================================================================
# Store / operator asset isolation
# ============================================================================


class StoreIsolationTest(DirectTradeTestBase):
    def test_list_moves_items_out_of_operator_bag(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20, produced=500, expires=5000)
        resp = self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.ORIGINAL)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        nora_total = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.nora_inv, item_id=1030)
        )
        self.assertEqual(nora_total, 10)

        store_total = sum(
            s.item_amount for s in
            VillageStoreInventorySlot.objects.filter(store=self.store, item_id=1030)
        )
        self.assertEqual(store_total, 10)

    def test_operator_wallet_unchanged_on_buy(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.ORIGINAL)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)

        # Location authorization belongs to Unity; the public projection still
        # works even when the caller's persisted position is elsewhere.
        self._set_location(10011, "MARKET_STREET")
        nora_money_before = self.nora_inv.money
        resp = self._buy(10011, listing.listing_id, 2)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.nora_inv.refresh_from_db()
        self.assertEqual(self.nora_inv.money, nora_money_before)


# ============================================================================
# Operator-only permissions
# ============================================================================


class OperatorPermissionTest(DirectTradeTestBase):
    def test_non_operator_cannot_list(self):
        # Lucy (10014) is an operator, but of RESTAURANT_STORE, not GENERAL_STORE.
        self._set_location(10014, "GENERAL_STORE")
        self._give_item(self.lucy_inv, 1030, 10)
        resp = self._list(10014, "GENERAL_STORE", 1030, 5, PriceMode.ORIGINAL)
        self.assertEqual(resp["result_code"], ResultCode.TRADE_STORE_FORBIDDEN)

    def test_non_operator_cannot_update_price(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.ORIGINAL)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)

        self._set_location(10014, "GENERAL_STORE")
        resp = commit_economy_action(_payload(
            "UPDATE_STORE_PRICE", 10014, "GENERAL_STORE",
            {"listing_id": str(listing.listing_id), "price_mode": PriceMode.CUSTOM,
             "custom_unit_price": 9},
        ))
        self.assertEqual(resp["result_code"], ResultCode.TRADE_STORE_FORBIDDEN)

    def test_non_operator_cannot_delist(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.ORIGINAL)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)

        self._set_location(10014, "GENERAL_STORE")
        resp = commit_economy_action(_payload(
            "DELIST_STORE_ITEM", 10014, "GENERAL_STORE",
            {"listing_id": str(listing.listing_id)},
        ))
        self.assertEqual(resp["result_code"], ResultCode.TRADE_STORE_FORBIDDEN)


# ============================================================================
# Price resolution
# ============================================================================


class OriginalPriceTest(DirectTradeTestBase):
    def test_original_reads_base_price(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        resp = self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.ORIGINAL)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["unit_price"], 8)  # BASE_PRICES[1030]

    def test_price_index_change_does_not_reprice(self):
        # Publish a different price index; ORIGINAL listing still uses BASE_PRICES.
        DailyPriceSnapshot.objects.create(
            day_index=0, prices={"1030": 99}, status=DaySnapshotStatus.PUBLISHED,
        )
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        resp = self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.ORIGINAL)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["unit_price"], 8)


class CustomPriceTest(DirectTradeTestBase):
    def test_custom_price_frozen(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        resp = self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.CUSTOM, custom=15)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)
        self.assertEqual(listing.unit_price, 15)
        self.assertEqual(listing.price_mode, PriceMode.CUSTOM)

    def test_invalid_custom_price_no_asset_change(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        nora_before = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.nora_inv, item_id=1030)
        )
        resp = self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.CUSTOM, custom=-3)
        self.assertEqual(resp["result_code"], ResultCode.TRADE_INVALID_CUSTOM_PRICE)
        nora_after = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.nora_inv, item_id=1030)
        )
        self.assertEqual(nora_before, nora_after)
        self.assertFalse(VillageStoreListing.objects.filter(store=self.store, item_id=1030).exists())

    def test_same_price_restock_merges_into_existing_listing(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)

        first = self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.CUSTOM, custom=15)
        self.assertEqual(first["result_code"], ResultCode.OK)
        first_id = first["data"]["listing_id"]

        second = self._list(10015, "GENERAL_STORE", 1030, 5, PriceMode.CUSTOM, custom=15)
        self.assertEqual(second["result_code"], ResultCode.OK)
        self.assertEqual(second["data"]["listing_id"], first_id)
        self.assertEqual(
            VillageStoreListing.objects.filter(
                store=self.store, item_id=1030, status=ListingStatus.ACTIVE.value
            ).count(),
            1,
        )
        self.assertEqual(
            sum(
                slot.item_amount
                for slot in VillageStoreInventorySlot.objects.filter(
                    store=self.store, listing_id=first_id
                )
            ),
            15,
        )

    def test_different_price_creates_new_listing(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)

        first = self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.CUSTOM, custom=15)
        second = self._list(10015, "GENERAL_STORE", 1030, 5, PriceMode.CUSTOM, custom=20)
        self.assertEqual(first["result_code"], ResultCode.OK)
        self.assertEqual(second["result_code"], ResultCode.OK)
        self.assertNotEqual(first["data"]["listing_id"], second["data"]["listing_id"])
        self.assertEqual(
            list(
                VillageStoreListing.objects.filter(
                    store=self.store, item_id=1030, status=ListingStatus.ACTIVE.value
                ).values_list("unit_price", flat=True).order_by("unit_price")
            ),
            [15, 20],
        )


class StoreItemEligibilityTest(DirectTradeTestBase):
    def test_any_existing_item_can_be_listed_with_custom_price(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1049, 12)
        resp = self._list(
            10015, "GENERAL_STORE", 1049, 12, PriceMode.CUSTOM, custom=7
        )
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["item_id"], 1049)
        self.assertEqual(resp["data"]["unit_price"], 7)

    def test_existing_tool_can_also_be_listed(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 4001, 1)
        resp = self._list(
            10015, "GENERAL_STORE", 4001, 1, PriceMode.CUSTOM, custom=60
        )
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["unit_price"], 60)

    def test_unknown_item_id_is_not_listable(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 9999, 5)
        nora_before = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.nora_inv, item_id=9999)
        )
        resp = self._list(10015, "GENERAL_STORE", 9999, 5, PriceMode.CUSTOM, custom=10)
        self.assertEqual(resp["result_code"], ResultCode.TRADE_ITEM_NOT_FOR_SALE)
        nora_after = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.nora_inv, item_id=9999)
        )
        self.assertEqual(nora_before, nora_after)
        self.assertFalse(VillageStoreListing.objects.filter(store=self.store, item_id=9999).exists())


# ============================================================================
# Batch preservation / delist
# ============================================================================


class BatchPreservationTest(DirectTradeTestBase):
    def test_list_preserves_batch_attributes(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 10, produced=500, expires=5000)
        self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.ORIGINAL)
        slot = VillageStoreInventorySlot.objects.get(store=self.store, item_id=1030)
        self.assertEqual(slot.produced_world_minute, 500)
        self.assertEqual(slot.expires_world_minute, 5000)
        self.assertEqual(slot.source_npc, self.nora)

    def test_tool_uses_survive_list_buy_and_delist(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 4001, 1)
        personal = NpcGlobalInventorySlot.objects.get(bag=self.nora_inv, item_id=4001)
        personal.remaining_uses = 17
        personal.save(update_fields=["remaining_uses"])

        self._list(10015, "GENERAL_STORE", 4001, 1, PriceMode.ORIGINAL)
        store_slot = VillageStoreInventorySlot.objects.get(store=self.store, item_id=4001)
        self.assertEqual(store_slot.remaining_uses, 17)

        listing = VillageStoreListing.objects.get(store=self.store, item_id=4001)
        self._set_location(10011, "GENERAL_STORE")
        self._buy(10011, listing.listing_id, 1)
        buyer_slot = NpcGlobalInventorySlot.objects.get(bag=self.bill_inv, item_id=4001)
        self.assertEqual(buyer_slot.remaining_uses, 17)

    def test_list_does_not_duplicate_assets(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 10)
        self._list(10015, "GENERAL_STORE", 1030, 10, PriceMode.ORIGINAL)
        total_items = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.nora_inv, item_id=1030)
        ) + sum(
            s.item_amount for s in
            VillageStoreInventorySlot.objects.filter(store=self.store, item_id=1030)
        )
        self.assertEqual(total_items, 10)


class DelistTest(DirectTradeTestBase):
    def test_delist_keeps_unsold_owned_by_store(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        self._list(10015, "GENERAL_STORE", 1030, 20, PriceMode.ORIGINAL)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)

        self._set_location(10011, "GENERAL_STORE")
        self._buy(10011, listing.listing_id, 3)

        resp = commit_economy_action(_payload(
            "DELIST_STORE_ITEM", 10015, "GENERAL_STORE",
            {"listing_id": str(listing.listing_id)},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["returned_amount"], 0)

        nora_total = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.nora_inv, item_id=1030)
        )
        self.assertEqual(nora_total, 0)
        self.assertEqual(sum(VillageStoreInventorySlot.objects.filter(store=self.store, listing__isnull=True).values_list("item_amount", flat=True)), 17)

        listing.refresh_from_db()
        self.assertEqual(listing.status, ListingStatus.DELISTED)


# ============================================================================
# Public inventory filtering
# ============================================================================


class PublicInventoryFilterTest(DirectTradeTestBase):
    def test_query_filters_non_active_and_expired(self):
        from items.models import Item

        Item.objects.update_or_create(pk=1030, defaults={"item_name": "玉米"})

        self._set_location(10015, "GENERAL_STORE")
        # List 30 corn whose store batch expires at minute 900 (future).
        NpcGlobalInventorySlot.objects.create(
            bag=self.nora_inv, slot_index=0, item_id=1030, item_amount=30,
            produced_world_minute=500, expires_world_minute=900,
        )
        self._list(10015, "GENERAL_STORE", 1030, 30, PriceMode.ORIGINAL)

        # Create and delist a second listing (non-ACTIVE).
        self._give_item(self.nora_inv, 1056, 10)
        self._list(10015, "GENERAL_STORE", 1056, 10, PriceMode.ORIGINAL)
        listing2 = VillageStoreListing.objects.get(store=self.store, item_id=1056)
        commit_economy_action(_payload(
            "DELIST_STORE_ITEM", 10015, "GENERAL_STORE",
            {"listing_id": str(listing2.listing_id)},
        ))

        self._set_location(10011, "GENERAL_STORE")
        code, data = query_store_inventory("GENERAL_STORE", 700)
        self.assertIsNone(code)
        self.assertEqual(data["operational_status"], self.store.status)
        self.assertEqual(data["inventory_status"], data["store_status"])
        items = {i["item_id"]: i for i in data["items"]}
        # 1056 was delisted -> excluded
        self.assertNotIn(1056, items)
        # 1030 available = unexpired batches only
        self.assertIn(1030, items)
        self.assertEqual(items[1030]["available_amount"], 30)
        self.assertEqual(items[1030]["name"], "玉米")

        # Advance time past the batch expiry -> excluded from available amount.
        self.time.world_minute = 950
        self.time.save(update_fields=["world_minute"])
        code, data = query_store_inventory("GENERAL_STORE", 950)
        items = {i["item_id"]: i for i in data["items"]}
        self.assertIn(1030, items)
        self.assertEqual(items[1030]["available_amount"], 0)


class StoreInventoryStatusTest(DirectTradeTestBase):
    """二跑复盘 R7：库存告警语义——有货即非 OUT_OF_STOCK，缺货是告警不是关门。"""

    def _stock(self, item_id, amount, reserved=0, expires=None):
        return VillageStoreInventorySlot.objects.create(
            store=self.store, item_id=item_id, item_amount=amount,
            reserved_amount=reserved, expires_world_minute=expires,
        )

    def test_out_of_stock_only_when_nothing_saleable(self):
        from village.phase3_direct_trade import store_inventory_status

        VillageStoreInventorySlot.objects.filter(store=self.store).delete()
        status, available = store_inventory_status(self.store, 700)
        self.assertEqual(status, "OUT_OF_STOCK")
        self.assertEqual(available, {})

    def test_partial_stock_is_low_stock_not_out_of_stock(self):
        from village.phase3_direct_trade import store_inventory_status

        # 只卖盐（1049）：SAFE_STOCKS 里其它 SKU 全缺 → LOW_STOCK（告警），不是关门。
        self._stock(1049, 12)
        status, available = store_inventory_status(self.store, 700)
        self.assertEqual(status, "LOW_STOCK")
        self.assertEqual(available, {1049: 12})

        # 预留量从可售数扣除；全过期后回到真正的 OUT_OF_STOCK。
        VillageStoreInventorySlot.objects.filter(store=self.store).delete()
        self._stock(1049, 12, reserved=8)
        status, available = store_inventory_status(self.store, 700)
        self.assertEqual(available, {1049: 4})
        self.assertEqual(status, "LOW_STOCK")

        slot = VillageStoreInventorySlot.objects.get(store=self.store)
        slot.expires_world_minute = 600
        slot.save(update_fields=["expires_world_minute"])
        status, available = store_inventory_status(self.store, 700)
        self.assertEqual(status, "OUT_OF_STOCK")

    def test_query_exposes_available_by_item(self):
        self._stock(1049, 12)
        code, data = query_store_inventory("GENERAL_STORE", 700)
        self.assertIsNone(code)
        self.assertEqual(data["inventory_status"], "LOW_STOCK")
        self.assertEqual(data["available_by_item"], {"1049": 12})


# ============================================================================
# Store purchase pricing & fee split
# ============================================================================


class BuyPricingTest(DirectTradeTestBase):
    def test_purchase_returns_actual_personal_food_batches_and_replay_keeps_ids(self):
        self._setup_listing(amount=3)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)
        store_slot = VillageStoreInventorySlot.objects.get(listing=listing)
        self._set_location(10011, "GENERAL_STORE")
        payload = _payload("BUY_FROM_STORE", 10011, "GENERAL_STORE",
            {"listing_id": str(listing.listing_id), "amount": 2})
        bought = commit_economy_action(payload)
        self.assertEqual(bought["result_code"], "OK")
        batch = bought["data"]["purchased_batches"][0]
        personal = NpcGlobalInventorySlot.objects.get(bag=self.bill_inv, instance_id=batch["food_instance_id"])
        self.assertEqual(personal.item_amount, 2)
        self.assertNotEqual(str(store_slot.instance_id), batch["food_instance_id"])
        self.assertEqual(batch["source"], "PERSONAL_INVENTORY")
        replay = commit_economy_action(payload)
        self.assertEqual(replay["data"]["purchased_batches"], bought["data"]["purchased_batches"])
        wrong = commit_economy_action(_payload("EAT", 10011, "GENERAL_STORE",
            {"source": batch["source"], "food_instance_id": str(store_slot.instance_id)}))
        self.assertEqual(wrong["data"]["failure_kind"], "PERSONAL_FOOD_INSTANCE_NOT_FOUND")
        eaten = commit_economy_action(_payload("EAT", 10011, "GENERAL_STORE",
            {"source": batch["source"], "food_instance_id": batch["food_instance_id"]}))
        self.assertEqual(eaten["result_code"], "OK")
        personal.refresh_from_db()
        self.assertEqual(personal.item_amount, 1)

    def _setup_listing(self, amount=20, price_mode=PriceMode.ORIGINAL, custom=None):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, amount)
        return self._list(10015, "GENERAL_STORE", 1030, amount, price_mode, custom=custom)

    def test_backend_computes_total_and_fee_split(self):
        resp = self._setup_listing(amount=20)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)

        self._set_location(10011, "GENERAL_STORE")
        buy = self._buy(10011, listing.listing_id, 10)
        self.assertEqual(buy["result_code"], ResultCode.OK)
        # gross = 8 * 10 = 80, raw fee = floor(80*500/10000)=4, fee=4, net=76
        self.assertEqual(buy["data"]["gross_amount"], 80)
        self.assertEqual(buy["data"]["fee_amount"], 4)
        self.assertEqual(buy["data"]["store_net_amount"], 76)
        self.assertEqual(buy["data"]["fee_amount"] + buy["data"]["store_net_amount"], 80)

    def test_minimum_fee_applies_and_fee_not_above_gross(self):
        self._setup_listing(amount=20)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)

        self._set_location(10011, "GENERAL_STORE")
        buy = self._buy(10011, listing.listing_id, 1)
        self.assertEqual(buy["result_code"], ResultCode.OK)
        # gross = 8, raw fee = floor(8*500/10000)=0, fee = max(1, 0)=1, net=7
        self.assertEqual(buy["data"]["gross_amount"], 8)
        self.assertEqual(buy["data"]["fee_amount"], 1)
        self.assertEqual(buy["data"]["store_net_amount"], 7)
        self.assertLessEqual(buy["data"]["fee_amount"], buy["data"]["gross_amount"])

    def test_buyer_pays_gross_only_and_fund_store_net_split(self):
        self._setup_listing(amount=20)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)

        self._set_location(10011, "GENERAL_STORE")
        bill_money_before = self.bill_inv.money
        fund_before = self.public_fund.balance
        store_balance_before = self.store_account.balance

        buy = self._buy(10011, listing.listing_id, 10)
        self.assertEqual(buy["result_code"], ResultCode.OK)

        self.bill_inv.refresh_from_db()
        self.public_fund.refresh_from_db()
        self.store_account.refresh_from_db()

        self.assertEqual(self.bill_inv.money, bill_money_before - 80)
        self.assertEqual(self.store_account.balance, store_balance_before + 76)
        self.assertEqual(self.public_fund.balance, fund_before + 4)

        # Fund conservation: buyer delta + store delta + public fund delta == 0
        self.assertEqual((-80) + 76 + 4, 0)
        self.assertEqual(buy["external_delta"], 0)


# ============================================================================
# Fee / public fund failure rollback
# ============================================================================


class FeeFailureTest(DirectTradeTestBase):
    def _setup_listing(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        self._list(10015, "GENERAL_STORE", 1030, 20, PriceMode.ORIGINAL)
        return VillageStoreListing.objects.get(store=self.store, item_id=1030)

    def test_invalid_fee_config_rejected(self):
        # The DB CheckConstraint (village_store_fee_bps_in_range) already blocks
        # out-of-range values at the persistence layer; this asserts the
        # application-level guard raises for every illegal policy shape.
        for bad_bps in (10001, -1):
            with self.assertRaises(ValueError):
                _compute_fee(100, bad_bps, 1)
        with self.assertRaises(ValueError):
            _compute_fee(100, 500, -1)

    def test_missing_public_fund_rolls_back(self):
        listing = self._setup_listing()
        VillageAccount.objects.filter(account_code="PUBLIC_FUND").delete()

        self._set_location(10011, "GENERAL_STORE")
        bill_money_before = self.bill_inv.money
        resp = self._buy(10011, listing.listing_id, 5)
        self.assertEqual(resp["result_code"], ResultCode.TRADE_PUBLIC_FUND_NOT_FOUND)
        self.bill_inv.refresh_from_db()
        self.assertEqual(self.bill_inv.money, bill_money_before)

    def test_operator_cannot_buy_own_store(self):
        listing = self._setup_listing()
        self._set_location(10015, "GENERAL_STORE")
        resp = self._buy(10015, listing.listing_id, 5)
        self.assertEqual(resp["result_code"], ResultCode.TRADE_STORE_FORBIDDEN)

    def test_insufficient_store_stock_rolls_back(self):
        # Store has 3 items but the buyer asks for 5.
        listing = self._setup_listing()
        # Reduce the store batch to 3 by selling 17 via an earlier buy.
        self._set_location(10011, "GENERAL_STORE")
        first = self._buy(10011, listing.listing_id, 17)
        self.assertEqual(first["result_code"], ResultCode.OK)

        store_slots_before = {
            s.instance_id: s.item_amount
            for s in VillageStoreInventorySlot.objects.filter(
                store=self.store, item_id=1030
            )
        }
        self.store_account.refresh_from_db()
        self.public_fund.refresh_from_db()
        self.bill_inv.refresh_from_db()
        account_before = self.store_account.balance
        fund_before = self.public_fund.balance
        bill_money_before = self.bill_inv.money

        resp = self._buy(10011, listing.listing_id, 5)
        self.assertEqual(resp["result_code"], ResultCode.TRADE_INSUFFICIENT_ITEM)

        # No partial deduction, no money movement.
        store_slots_after = {
            s.instance_id: s.item_amount
            for s in VillageStoreInventorySlot.objects.filter(
                store=self.store, item_id=1030
            )
        }
        self.assertEqual(store_slots_after, store_slots_before)
        self.store_account.refresh_from_db()
        self.public_fund.refresh_from_db()
        self.bill_inv.refresh_from_db()
        self.assertEqual(self.store_account.balance, account_before)
        self.assertEqual(self.public_fund.balance, fund_before)
        self.assertEqual(self.bill_inv.money, bill_money_before)


# ============================================================================
# Trade offers (propose / respond / cancel)
# ============================================================================


class TradeOfferTest(DirectTradeTestBase):
    def _setup_seller_items(self, amount=10):
        self._give_item(self.jack_inv, 1030, amount, produced=500, expires=5000)

    def test_propose_creates_pending_offer(self):
        resp = self._propose(10011, 10012, 1030, 5, 40)
        self.assertEqual(resp["result_code"], ResultCode.OK)
        offer = VillageTradeOffer.objects.get(offer_id=resp["data"]["offer_id"])
        self.assertEqual(offer.status, TradeOfferStatus.PENDING)
        self.assertEqual(offer.buyer_npc.agent_id, "10011")
        self.assertEqual(offer.seller_npc.agent_id, "10012")

    def test_sell_side_maps_proposer_as_seller_and_counterparty_responds(self):
        self._setup_seller_items()
        resp = self._propose(10012, 10011, 1030, 2, 16, side="SELL")
        self.assertEqual(resp["result_code"], ResultCode.OK)
        offer = VillageTradeOffer.objects.get(offer_id=resp["data"]["offer_id"])
        self.assertEqual(offer.proposer_npc.agent_id, "10012")
        self.assertEqual(offer.buyer_npc.agent_id, "10011")
        self.assertEqual(offer.seller_npc.agent_id, "10012")
        self.assertEqual(self._respond(10011, offer.offer_id, "ACCEPT")["result_code"], ResultCode.OK)

    def test_propose_target_not_found(self):
        resp = self._propose(10011, 99999, 1030, 5, 40)
        self.assertEqual(resp["result_code"], ResultCode.TRADE_TARGET_NOT_FOUND)

    def test_propose_self_target_forbidden(self):
        resp = self._propose(10011, 10011, 1030, 5, 40)
        self.assertEqual(resp["result_code"], ResultCode.TRADE_TARGET_NOT_FOUND)

    def test_accept_transfers_and_bumps_versions(self):
        self._setup_seller_items()
        prop = self._propose(10011, 10012, 1030, 5, 40)
        offer_id = prop["data"]["offer_id"]

        bill_money_before = self.bill_inv.money
        jack_money_before = self.jack_inv.money
        bill_ver_before = self.bill_inv.asset_version
        jack_ver_before = self.jack_inv.asset_version

        resp = self._respond(10012, offer_id, "ACCEPT")
        self.assertEqual(resp["result_code"], ResultCode.OK)

        self.bill_inv.refresh_from_db()
        self.jack_inv.refresh_from_db()
        self.assertEqual(self.bill_inv.money, bill_money_before - 40)
        self.assertEqual(self.jack_inv.money, jack_money_before + 40)
        self.assertEqual(self.bill_inv.asset_version, bill_ver_before + 1)
        self.assertEqual(self.jack_inv.asset_version, jack_ver_before + 1)

        bill_items = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.bill_inv, item_id=1030)
        )
        self.assertEqual(bill_items, 5)

        offer = VillageTradeOffer.objects.get(offer_id=offer_id)
        self.assertEqual(offer.status, TradeOfferStatus.ACCEPTED)
        self.assertEqual(resp["data"]["buyer_npc_id"], "10011")

    def test_reject_result_identifies_buyer_for_notification(self):
        prop = self._propose(10011, 10012, 1030, 1, 5)
        offer_id = prop["data"]["offer_id"]

        resp = self._respond(10012, offer_id, "REJECT")
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertEqual(resp["data"]["buyer_npc_id"], "10011")
        self.assertEqual(resp["data"]["status"], TradeOfferStatus.REJECTED)

    def test_non_recipient_cannot_respond(self):
        resp = self._propose(10011, 10012, 1030, 5, 40)
        offer_id = resp["data"]["offer_id"]
        # Bill (buyer) is not the recipient; only Jack (seller) may respond.
        resp2 = self._respond(10011, offer_id, "ACCEPT")
        self.assertEqual(resp2["result_code"], ResultCode.TRADE_RESPONSE_FORBIDDEN)

    def test_respond_with_extra_terms_rejected(self):
        self._setup_seller_items()
        prop = self._propose(10011, 10012, 1030, 5, 40)
        offer_id = prop["data"]["offer_id"]
        resp = commit_economy_action(_payload(
            "RESPOND_TRADE", 10012, "MARKET_STREET",
            {"offer_id": str(offer_id), "decision": "ACCEPT", "gold_amount": 999},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_only_initiator_can_cancel_and_duplicate_cancel_idempotent(self):
        resp = self._propose(10011, 10012, 1030, 5, 40)
        offer_id = resp["data"]["offer_id"]

        # Non-initiator cannot cancel.
        wrong = self._cancel_offer(10012, offer_id)
        self.assertEqual(wrong["result_code"], ResultCode.TRADE_CANCEL_FORBIDDEN)

        # Initiator cancels (fixed payload so the same request_id replays).
        cancel_payload = {
            "schema_version": VILLAGE_ECONOMY_ACTION_V1,
            "command_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
            "command_name": "CANCEL_TRADE_OFFER",
            "npc_id": 10011,
            "completed_world_minute": 700,
            "time_revision": 700,
            "current_location_code": "MARKET_STREET",
            "params": {"offer_id": str(offer_id)},
        }
        cancel = commit_economy_action(cancel_payload)
        self.assertEqual(cancel["result_code"], ResultCode.OK)

        # Same request_id replays.
        replay = commit_economy_action(cancel_payload)
        self.assertEqual(replay["result_code"], ResultCode.OK)
        self.assertTrue(replay["replayed"])

        offer = VillageTradeOffer.objects.get(offer_id=offer_id)
        self.assertEqual(offer.status, TradeOfferStatus.CANCELLED)

    def test_reaccept_resolved_offer_fails(self):
        self._setup_seller_items()
        prop = self._propose(10011, 10012, 1030, 5, 40)
        offer_id = prop["data"]["offer_id"]
        self._respond(10012, offer_id, "ACCEPT")

        resp2 = self._respond(10012, offer_id, "ACCEPT")
        self.assertEqual(resp2["result_code"], ResultCode.TRADE_OFFER_ALREADY_RESOLVED)

    def test_expired_offer_fails(self):
        self._setup_seller_items()
        prop = self._propose(10011, 10012, 1030, 5, 40, expires=800)
        offer_id = prop["data"]["offer_id"]

        self.time.world_minute = 900
        self.time.save(update_fields=["world_minute"])

        resp = self._respond(10012, offer_id, "ACCEPT")
        self.assertEqual(resp["result_code"], ResultCode.TRADE_OFFER_EXPIRED)
        offer = VillageTradeOffer.objects.get(offer_id=offer_id)
        self.assertEqual(offer.status, TradeOfferStatus.EXPIRED)

    def test_not_colocated_is_deferred_to_unity(self):
        self._setup_seller_items()
        prop = self._propose(10011, 10012, 1030, 5, 40)
        offer_id = prop["data"]["offer_id"]

        # Unity rejects this before settlement; Backend only settles the
        # already-authorized economy request and does not re-check location.
        self._set_location(10012, "RESTAURANT")
        resp = self._respond(10012, offer_id, "ACCEPT")
        self.assertEqual(resp["result_code"], ResultCode.OK)

    def test_insufficient_item_rolls_back(self):
        # Seller has 3 items but offer is for 5.
        self._give_item(self.jack_inv, 1030, 3)
        prop = self._propose(10011, 10012, 1030, 5, 40)
        offer_id = prop["data"]["offer_id"]

        bill_money_before = self.bill_inv.money
        resp = self._respond(10012, offer_id, "ACCEPT")
        self.assertEqual(resp["result_code"], ResultCode.TRADE_INSUFFICIENT_ITEM)

        # No partial deduction: seller's 3 items remain intact, buyer money unchanged.
        self.bill_inv.refresh_from_db()
        self.assertEqual(self.bill_inv.money, bill_money_before)
        jack_items = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.jack_inv, item_id=1030)
        )
        self.assertEqual(jack_items, 3)

    def test_insufficient_money_rolls_back(self):
        self._setup_seller_items()
        prop = self._propose(10011, 10012, 1030, 5, 99999)
        offer_id = prop["data"]["offer_id"]

        bill_money_before = self.bill_inv.money
        resp = self._respond(10012, offer_id, "ACCEPT")
        self.assertEqual(resp["result_code"], ResultCode.TRADE_INSUFFICIENT_MONEY)

        # No item transfer and no money change on failure.
        self.bill_inv.refresh_from_db()
        self.assertEqual(self.bill_inv.money, bill_money_before)
        jack_items = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.jack_inv, item_id=1030)
        )
        self.assertEqual(jack_items, 10)


# ============================================================================
# Idempotency / version / audit / ledger
# ============================================================================


class IdempotencyAndVersionTest(DirectTradeTestBase):
    def test_same_request_id_single_settlement(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        self._list(10015, "GENERAL_STORE", 1030, 20, PriceMode.ORIGINAL)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)

        self._set_location(10011, "GENERAL_STORE")
        payload = {
            "schema_version": VILLAGE_ECONOMY_ACTION_V1,
            "command_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
            "command_name": "BUY_FROM_STORE",
            "npc_id": 10011,
            "completed_world_minute": 700,
            "time_revision": 700,
            "current_location_code": "GENERAL_STORE",
            "params": {"listing_id": str(listing.listing_id), "amount": 5},
        }
        first = commit_economy_action(payload)
        self.assertEqual(first["result_code"], ResultCode.OK)
        replay = commit_economy_action(payload)
        self.assertEqual(replay["result_code"], ResultCode.OK)
        self.assertTrue(replay["replayed"])

        # Only one settlement happened.
        self.bill_inv.refresh_from_db()
        self.assertEqual(self.bill_inv.money, 200 - 40)

    def test_versions_bump_after_list_and_buy(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        nora_ver = self.nora_inv.asset_version
        store_ver = self.store.inventory_version
        self._list(10015, "GENERAL_STORE", 1030, 20, PriceMode.ORIGINAL)
        self.nora_inv.refresh_from_db()
        self.store.refresh_from_db()
        self.assertEqual(self.nora_inv.asset_version, nora_ver + 1)
        self.assertEqual(self.store.inventory_version, store_ver + 1)

        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)
        self._set_location(10011, "GENERAL_STORE")
        bill_ver = self.bill_inv.asset_version
        fund_ver = self.public_fund.asset_version
        self._buy(10011, listing.listing_id, 5)
        self.bill_inv.refresh_from_db()
        self.public_fund.refresh_from_db()
        self.assertEqual(self.bill_inv.asset_version, bill_ver + 1)
        self.assertEqual(self.public_fund.asset_version, fund_ver + 1)


class RationLimitTradeTest(DirectTradeTestBase):
    def test_story_ration_limit_rejects_second_buy(self):
        cfg = VillageSimulationConfig.objects.get(config_key="default")
        cfg.village_story_enabled = True
        cfg.save(update_fields=["village_story_enabled"])
        arc = VillageStoryArc.objects.create(
            arc_code="BROKEN_TRADE_ROUTE_V1", current_phase="EXECUTION",
            current_plan="RATION_AND_WAIT", start_day=0, phase_entered_day=0,
        )
        VillageStoryFact.objects.create(
            arc=arc, store_limits_enabled=True, ration_daily_limits={"1030": 1},
        )
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        self._list(10015, "GENERAL_STORE", 1030, 20, PriceMode.ORIGINAL)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)
        self._set_location(10011, "GENERAL_STORE")

        first = self._buy(10011, listing.listing_id, 1)
        self.assertEqual(first["result_code"], ResultCode.OK)
        self.bill_inv.refresh_from_db()
        second = self._buy(
            10011, listing.listing_id, 1,
            exp_store=self.store.inventory_version,
            exp_buyer=self.bill_inv.asset_version,
        )
        self.assertEqual(second["result_code"], "STORY_RATION_LIMIT_REACHED")
        listing.refresh_from_db()
        self.assertEqual(
            sum(slot.item_amount for slot in listing.inventory_slots.all()), 19,
        )


class AuditAndLedgerTest(DirectTradeTestBase):
    def test_audit_reconciles_store_fee(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        self._list(10015, "GENERAL_STORE", 1030, 20, PriceMode.ORIGINAL)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)

        self._set_location(10011, "GENERAL_STORE")
        self._buy(10011, listing.listing_id, 10)  # fee = 4

        # Move Mark to RECORD_HALL and audit.
        self._set_location(10013, "RECORD_HALL")
        resp = commit_economy_action(_payload(
            "AUDIT_PUBLIC_FUND", 10013, "RECORD_HALL", {"day_index": 0},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)
        audit = resp["effects"]["audit"]
        self.assertTrue(audit["balance_matches"])
        # Public fund is 500 + 4 fee = 504 at recompute time (before the audit's
        # own daily meal-reference salary is deducted).
        self.assertEqual(audit["expected_balance"], 504)
        self.assertEqual(audit["actual_balance"], 504)

    def test_receipt_ledger_event_written_on_buy(self):
        self._set_location(10015, "GENERAL_STORE")
        self._give_item(self.nora_inv, 1030, 20)
        self._list(10015, "GENERAL_STORE", 1030, 20, PriceMode.ORIGINAL)
        listing = VillageStoreListing.objects.get(store=self.store, item_id=1030)

        self._set_location(10011, "GENERAL_STORE")
        rid = uuid.uuid4()
        buy = self._buy(10011, listing.listing_id, 5, request_id=rid)
        self.assertEqual(buy["result_code"], ResultCode.OK)

        self.assertTrue(VillageActionReceipt.objects.filter(request_id=rid).exists())
        self.assertTrue(EconomicLedgerEntry.objects.filter(request_id=rid).exists())
        self.assertTrue(VillageWorldEvent.objects.filter(request_id=rid).exists())

        ledger = EconomicLedgerEntry.objects.get(request_id=rid)
        self.assertEqual(ledger.entry_type, "BUY_FROM_STORE")
        self.assertEqual(ledger.changes["PUBLIC_FUND"]["money"], 2)  # 5% of 40
        self.assertEqual(ledger.external_delta, 0)


# ============================================================================
# Feature flag & legacy migration
# ============================================================================


class FeatureFlagTest(DirectTradeTestBase):
    def test_direct_trade_disabled_returns_feature_disabled(self):
        cfg = VillageSimulationConfig.objects.get(config_key="default")
        cfg.village_direct_trade_enabled = False
        cfg.save(update_fields=["village_direct_trade_enabled"])

        self._give_item(self.jack_inv, 1030, 5)
        resp = self._propose(10011, 10012, 1030, 5, 40)
        self.assertEqual(resp["result_code"], ResultCode.P3_FEATURE_DISABLED)


class LegacyMigrationTest(DirectTradeTestBase):
    def test_migrate_cancels_open_matched_and_active(self):
        # OPEN order
        open_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(), side="SELL", npc=self.bill, item_id=1030,
            amount=5, unit_price=8, delivery_point_code="MARKET_STREET",
            expires_world_minute=5000, status="OPEN",
        )
        # MATCHED pair + ACTIVE delivery
        buy_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(), side="BUY", npc=self.jack, item_id=1030,
            amount=5, unit_price=8, delivery_point_code="MARKET_STREET",
            expires_world_minute=5000, status="MATCHED",
        )
        sell_order = NpcTradeOrder.objects.create(
            request_id=uuid.uuid4(), side="SELL", npc=self.bill, item_id=1030,
            amount=5, unit_price=8, delivery_point_code="MARKET_STREET",
            expires_world_minute=5000, status="MATCHED",
        )
        slot = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=1030, item_amount=10,
        )
        delivery = NpcTradeDelivery.objects.create(
            buyer_order=buy_order, seller_order=sell_order,
            seller_slot_instance_id=slot.instance_id,
            reserved_amount=5, reserved_money=40, delivery_point_code="MARKET_STREET",
            expires_world_minute=5000, status="ACTIVE",
        )

        summary = migrate_legacy_trade_orders()
        self.assertEqual(summary["open_orders_cancelled"], 1)
        self.assertEqual(summary["matched_orders_cancelled"], 2)
        self.assertEqual(summary["active_deliveries_cancelled"], 1)

        open_order.refresh_from_db()
        buy_order.refresh_from_db()
        sell_order.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(open_order.status, "CANCELLED")
        self.assertEqual(buy_order.status, "CANCELLED")
        self.assertEqual(sell_order.status, "CANCELLED")
        self.assertEqual(delivery.status, "CANCELLED")

        # Reservations released: no ACTIVE deliveries remain.
        self.assertFalse(NpcTradeDelivery.objects.filter(status="ACTIVE").exists())
