"""Tests for Phase 3 store / direct-trade models and their seed migration."""

import importlib
import uuid

from django.apps import apps as global_apps
from django.db import IntegrityError, transaction
from django.test import TestCase

from inventory.models import NpcDefinition
from village.models import (
    VillageStore,
    VillageStoreAccount,
    VillageStoreInventorySlot,
    VillageStoreListing,
    VillageTradeOffer,
)


def _make_store(store_code="TEST_STORE", operator=None, **overrides):
    if operator is None:
        operator = NpcDefinition.objects.create(agent_id="90001", name="op")
    defaults = {
        "store_code": store_code,
        "location_code": "GENERAL_STORE",
        "operator_npc": operator,
        "display_name": "测试商店",
    }
    defaults.update(overrides)
    return VillageStore.objects.create(**defaults)


class VillageStoreModelTest(TestCase):
    """VillageStore fee-config constraints."""

    def test_create_store_defaults(self):
        store = _make_store()
        self.assertEqual(store.status, "OPEN")
        self.assertEqual(store.transaction_fee_bps, 500)
        self.assertEqual(store.minimum_fee, 1)
        self.assertEqual(store.inventory_version, 0)

    def test_fee_bps_upper_bound(self):
        store = _make_store()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                store.transaction_fee_bps = 10001
                store.save(update_fields=["transaction_fee_bps"])

    def test_fee_bps_lower_bound(self):
        store = _make_store()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                store.transaction_fee_bps = -1
                store.save(update_fields=["transaction_fee_bps"])

    def test_minimum_fee_non_negative(self):
        store = _make_store()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                store.minimum_fee = -1
                store.save(update_fields=["minimum_fee"])


class VillageStoreAccountModelTest(TestCase):
    """VillageStoreAccount constraints."""

    def test_create_account_defaults(self):
        store = _make_store()
        acc = VillageStoreAccount.objects.create(store=store)
        self.assertEqual(acc.balance, 0)
        self.assertEqual(acc.account_version, 0)

    def test_balance_non_negative(self):
        store = _make_store()
        acc = VillageStoreAccount.objects.create(store=store, balance=0)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                acc.balance = -1
                acc.save(update_fields=["balance"])

    def test_one_account_per_store(self):
        store = _make_store()
        VillageStoreAccount.objects.create(store=store)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VillageStoreAccount.objects.create(store=store)


class VillageStoreListingModelTest(TestCase):
    """VillageStoreListing constraints."""

    @classmethod
    def setUpTestData(cls):
        cls.operator = NpcDefinition.objects.create(agent_id="90002", name="listing_op")
        cls.store = _make_store(store_code="LISTING_STORE", operator=cls.operator)

    def _listing(self, item_id=1030, status="ACTIVE", unit_price=8):
        return VillageStoreListing(
            store=self.store,
            item_id=item_id,
            unit_price=unit_price,
            status=status,
            listed_by_npc=self.operator,
            created_request_id=uuid.uuid4(),
            listed_world_minute=100,
        )

    def test_unit_price_must_be_positive(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._listing(unit_price=0).save()

    def test_multiple_active_listings_per_store_item_are_allowed(self):
        self._listing(item_id=1030).save()
        self._listing(item_id=1030, unit_price=15).save()
        self.assertEqual(
            VillageStoreListing.objects.filter(
                store=self.store, item_id=1030, status="ACTIVE"
            ).count(),
            2,
        )

    def test_multiple_non_active_listings_allowed(self):
        self._listing(item_id=1030, status="SOLD_OUT").save()
        self._listing(item_id=1030, status="DELISTED").save()
        self.assertEqual(
            VillageStoreListing.objects.filter(store=self.store, item_id=1030).count(),
            2,
        )


class VillageStoreInventorySlotModelTest(TestCase):
    """VillageStoreInventorySlot constraints."""

    def setUp(self):
        self.operator = NpcDefinition.objects.create(agent_id="90003", name="slot_op")
        self.store = _make_store(store_code="SLOT_STORE", operator=self.operator)

    def test_create_slot(self):
        slot = VillageStoreInventorySlot.objects.create(
            store=self.store,
            item_id=1030,
            item_amount=5,
            source_npc=self.operator,
        )
        self.assertEqual(slot.item_amount, 5)
        self.assertEqual(slot.reserved_amount, 0)

    def test_item_amount_non_negative(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VillageStoreInventorySlot.objects.create(
                    store=self.store, item_id=1030, item_amount=-1
                )

    def test_reserved_amount_non_negative(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VillageStoreInventorySlot.objects.create(
                    store=self.store, item_id=1030, item_amount=1, reserved_amount=-1
                )


class VillageTradeOfferModelTest(TestCase):
    """VillageTradeOffer constraints."""

    @classmethod
    def setUpTestData(cls):
        cls.buyer = NpcDefinition.objects.create(agent_id="90010", name="buyer")
        cls.seller = NpcDefinition.objects.create(agent_id="90011", name="seller")

    def _offer(self, buyer=None, seller=None, amount=5, gold_amount=10, **overrides):
        defaults = {
            "buyer_npc": buyer or self.buyer,
            "seller_npc": seller or self.seller,
            "proposer_npc": buyer or self.buyer,
            "item_id": 1030,
            "amount": amount,
            "gold_amount": gold_amount,
            "location_code": "MARKET_STREET",
            "expires_world_minute": 1000,
            "created_request_id": uuid.uuid4(),
            "created_world_minute": 100,
        }
        defaults.update(overrides)
        return VillageTradeOffer(**defaults)

    def test_create_offer_defaults(self):
        offer = self._offer()
        offer.save()
        self.assertEqual(offer.status, "PENDING")
        self.assertIsNone(offer.resolved_request_id)

    def test_amount_must_be_positive(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._offer(amount=0).save()

    def test_gold_amount_must_be_positive(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._offer(gold_amount=0).save()

    def test_buyer_and_seller_must_differ(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._offer(seller=self.buyer).save()

    def test_created_request_id_unique(self):
        rid = uuid.uuid4()
        self._offer(created_request_id=rid).save()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._offer(created_request_id=rid).save()

    def test_resolved_request_id_unique_when_not_null(self):
        rid = uuid.uuid4()
        self._offer(resolved_request_id=rid, status="ACCEPTED").save()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._offer(resolved_request_id=rid, status="ACCEPTED").save()

    def test_multiple_null_resolved_request_id_allowed(self):
        self._offer().save()
        self._offer().save()
        self.assertEqual(VillageTradeOffer.objects.count(), 2)


class StoreSeedMigrationTest(TestCase):
    """Migration 0011 seeds the two stores (+ accounts) idempotently."""

    migration_module = importlib.import_module("village.migrations.0011_phase3_store_models")

    def setUp(self):
        # This tests 0011's initial seed, not 0039's later restaurant account
        # adjustment. Start with no stores instead of depending on migration order.
        VillageStore.objects.filter(store_code__in=["GENERAL_STORE", "RESTAURANT_STORE"]).delete()

    @classmethod
    def setUpTestData(cls):
        NpcDefinition.objects.get_or_create(
            agent_id="10015",
            defaults={"name": "nora", "is_test_only": False},
        )
        NpcDefinition.objects.get_or_create(
            agent_id="10014",
            defaults={"name": "lucy", "is_test_only": False},
        )

    def test_seed_creates_stores_accounts_and_action_configs(self):
        self.migration_module.seed_stores_and_action_configs(global_apps, None)

        general = VillageStore.objects.get(store_code="GENERAL_STORE")
        self.assertEqual(general.location_code, "GENERAL_STORE")
        self.assertEqual(general.operator_npc.agent_id, "10015")
        self.assertEqual(general.transaction_fee_bps, 500)
        self.assertEqual(general.minimum_fee, 1)

        restaurant = VillageStore.objects.get(store_code="RESTAURANT_STORE")
        self.assertEqual(restaurant.location_code, "RESTAURANT")
        self.assertEqual(restaurant.operator_npc.agent_id, "10014")
        self.assertEqual(restaurant.transaction_fee_bps, 500)
        self.assertEqual(restaurant.minimum_fee, 1)

        for store in (general, restaurant):
            account = VillageStoreAccount.objects.get(store=store)
            self.assertEqual(account.balance, 0)
            self.assertEqual(account.account_version, 0)

    def test_seed_is_idempotent(self):
        self.migration_module.seed_stores_and_action_configs(global_apps, None)
        self.migration_module.seed_stores_and_action_configs(global_apps, None)

        self.assertEqual(VillageStore.objects.filter(store_code__in=["GENERAL_STORE", "RESTAURANT_STORE"]).count(), 2)
        self.assertEqual(VillageStoreAccount.objects.filter(store__store_code__in=["GENERAL_STORE", "RESTAURANT_STORE"]).count(), 2)
