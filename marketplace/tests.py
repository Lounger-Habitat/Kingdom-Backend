from datetime import timedelta
import uuid

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from account.models import GameAccount
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType
from marketplace.models import MarketListing, MarketListingSource, MarketListingStatus, MarketTransaction
from marketplace.serializers import serialize_listing, serialize_transaction
from marketplace.services import (
    buy_listing,
    buy_listing_for_npc,
    create_listing,
    create_npc_listing,
    expire_due_listings,
    get_npc_history,
    get_npc_listings,
)


class MarketplaceNpcSupportTests(TestCase):
    def setUp(self):
        self.item = Item.objects.create(item_id=9001, item_name="测试苹果", item_type=ItemType.INGREDIENT)

        self.player_seller = GameAccount.objects.create(username="seller", password="x")
        self.player_buyer = GameAccount.objects.create(username="buyer", password="x")
        self.player_seller_bag = InventoryBag.objects.create(
            player=self.player_seller, character_id=self.player_seller.username, money=10
        )
        self.player_buyer_bag = InventoryBag.objects.create(
            player=self.player_buyer, character_id=self.player_buyer.username, money=200
        )
        self.player_slot = InventorySlot.objects.create(
            bag=self.player_seller_bag,
            slot_index=0,
            instance_id=uuid.uuid4(),
            item_id=self.item.item_id,
            item_amount=5,
            item_bag_name="测试苹果_0",
        )

        self.npc_seller = NpcDefinition.objects.create(agent_id="NPC_MARKET_1", name="bill")
        self.npc_buyer = NpcDefinition.objects.create(agent_id="NPC_MARKET_2", name="jack")
        self.npc_seller_bag = NpcGlobalInventory.objects.create(npc=self.npc_seller, money=20)
        self.npc_buyer_bag = NpcGlobalInventory.objects.create(npc=self.npc_buyer, money=300)
        self.npc_slot = NpcGlobalInventorySlot.objects.create(
            bag=self.npc_seller_bag,
            slot_index=0,
            instance_id=uuid.uuid4(),
            item_id=self.item.item_id,
            item_amount=4,
            item_bag_name="测试苹果_0",
        )

    def test_player_marketplace_flow_still_works(self):
        listing, err = create_listing(self.player_seller, self.player_slot.instance_id, 2, 25)
        self.assertIsNone(err)
        self.player_slot.refresh_from_db()
        self.assertEqual(self.player_slot.item_amount, 3)

        result, err = buy_listing(self.player_buyer, listing.pk, 2)
        self.assertIsNone(err)
        self.assertEqual(result["listing"].status, MarketListingStatus.SOLD_OUT)

        self.player_buyer_bag.refresh_from_db()
        self.player_seller_bag.refresh_from_db()
        self.assertEqual(self.player_buyer_bag.money, 150)
        self.assertEqual(self.player_seller_bag.money, 56)
        self.assertEqual(self.player_buyer_bag.slots.get(item_id=self.item.item_id).item_amount, 2)

    def test_npc_listing_buy_and_history(self):
        listing, err = create_npc_listing(
            self.npc_seller,
            self.npc_slot.instance_id,
            3,
            30,
            expected_asset_version=self.npc_seller_bag.asset_version,
        )
        self.assertIsNone(err)
        self.npc_seller_bag.refresh_from_db()
        self.assertEqual(self.npc_seller_bag.asset_version, 1)

        result, err = buy_listing_for_npc(
            self.npc_buyer,
            listing.pk,
            2,
            expected_asset_version=self.npc_buyer_bag.asset_version,
        )
        self.assertIsNone(err)

        self.npc_seller_bag.refresh_from_db()
        self.npc_buyer_bag.refresh_from_db()
        listing.refresh_from_db()

        self.assertEqual(self.npc_seller_bag.money, 75)
        self.assertEqual(self.npc_buyer_bag.money, 240)
        self.assertEqual(self.npc_seller_bag.asset_version, 2)
        self.assertEqual(self.npc_buyer_bag.asset_version, 1)
        self.assertEqual(listing.remaining_amount, 1)
        self.assertEqual(get_npc_listings(self.npc_seller).count(), 1)

        tx = get_npc_history(self.npc_buyer, role="buyer").get()
        self.assertEqual(tx.buyer_npc, self.npc_buyer)
        self.assertEqual(tx.seller_npc, self.npc_seller)
        self.assertEqual(serialize_listing(listing, self.npc_seller)["sellerName"], "bill")
        self.assertEqual(serialize_transaction(tx)["buyerName"], "jack")

    def test_expire_returns_items_to_npc_and_constraints_hold(self):
        listing, err = create_npc_listing(
            self.npc_seller,
            self.npc_slot.instance_id,
            2,
            15,
            expected_asset_version=self.npc_seller_bag.asset_version,
        )
        self.assertIsNone(err)
        listing.expires_at = timezone.now() - timedelta(minutes=1)
        listing.save(update_fields=["expires_at"])

        expired_ids = expire_due_listings()
        self.assertIn(listing.pk, expired_ids)

        self.npc_seller_bag.refresh_from_db()
        listing.refresh_from_db()
        self.assertEqual(listing.status, MarketListingStatus.EXPIRED)
        self.assertEqual(self.npc_seller_bag.asset_version, 2)
        self.assertEqual(self.npc_seller_bag.slots.get(item_id=self.item.item_id).item_amount, 4)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MarketListing.objects.create(
                    source_type=MarketListingSource.NPC,
                    seller=self.player_seller,
                    item_id=self.item.item_id,
                    total_amount=1,
                    remaining_amount=1,
                    price=10,
                    status=MarketListingStatus.ACTIVE,
                    item_snapshot={},
                )

        valid_listing = MarketListing.objects.create(
            source_type=MarketListingSource.PLAYER,
            seller=self.player_seller,
            item_id=self.item.item_id,
            total_amount=1,
            remaining_amount=1,
            price=10,
            status=MarketListingStatus.ACTIVE,
            item_snapshot={},
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MarketTransaction.objects.create(
                    listing=valid_listing,
                    source_type=MarketListingSource.PLAYER,
                    seller=self.player_seller,
                    buyer=self.player_buyer,
                    buyer_npc=self.npc_buyer,
                    item_id=self.item.item_id,
                    amount=1,
                    unit_price=10,
                    total_price=10,
                )
