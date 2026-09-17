"""Exercise forward AND backward data migrations using historical ORM models."""
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


BEFORE = [("inventory", "0027_lucy_metadata_and_review_actions"),
          ("marketplace", "0004_marketlisting_seller_npc_markettransaction_buyer_npc_and_more"),
          ("village", "0041_bill_farm_wild_harvest")]
AFTER = [("inventory", "0028_inventoryslotdish_inventoryslotreview_and_more"),
         ("marketplace", "0005_marketlistingdish_marketlistingreview_and_more"),
         ("village", "0042_villagestoreinventoryslotdish_and_more")]


class ComponentMigrationTests(TransactionTestCase):
    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self.migrate(AFTER)
        super().tearDown()

    def test_historical_player_npc_store_market_forward_reverse_forward(self):
        apps = self.migrate(BEFORE)
        npc = apps.get_model("inventory", "NpcDefinition").objects.create(agent_id="migration-test", name="test")
        npc_bag = apps.get_model("inventory", "NpcGlobalInventory").objects.create(npc=npc)
        bag = apps.get_model("inventory", "InventoryBag").objects.create(character_id="migration-test")
        store = apps.get_model("village", "VillageStore").objects.create(store_code="MIGRATION_TEST", display_name="test", location_code="RESTAURANT")
        attrs = dict(rated=True, rating_price=87, overall_score=99.5, ingredient="玉米×2", cook_time=120,
                     dish_quality=6, review_comment="测试评语", dish_metadata={"review_id": str(uuid.uuid4())})
        identity = uuid.uuid4()
        for name, owner in (("InventorySlot", bag), ("NpcGlobalInventorySlot", npc_bag)):
            Model = apps.get_model("inventory", name)
            Model.objects.create(bag=owner, slot_index=3, instance_id=identity, item_id=1030, item_amount=7,
                                 remaining_uses=-1, expires_world_minute=900, **attrs)
            # Preserve zero, unknown and invalid sentinel values without coercion.
            for index, uses in enumerate((0, None, -2, 8), start=4):
                Model.objects.create(bag=owner, slot_index=index, item_id=4001, item_amount=1, remaining_uses=uses)
        stock = apps.get_model("village", "VillageStoreInventorySlot").objects.create(
            store=store, item_id=1030, item_amount=3, remaining_uses=0, reserved_amount=2,
            source_slot_instance_id=identity, item_attributes=dict(attrs, extra={"keep": True}))
        listing = apps.get_model("marketplace", "MarketListing").objects.create(
            source_type="system", item_id=1030, price=87, remaining_uses=8,
            source_instance_id=identity, item_snapshot={"dishMetadata": attrs["dish_metadata"]},
            **{key: value for key, value in attrs.items() if key != "dish_metadata"})
        stock_pk, listing_pk = stock.pk, listing.pk
        self.migrate(AFTER)
        from inventory.models import InventorySlot, NpcGlobalInventorySlot
        from village.models import VillageStoreInventorySlot
        from marketplace.models import MarketListing
        for Model, owner in ((InventorySlot, bag), (NpcGlobalInventorySlot, npc_bag)):
            row = Model.objects.get(bag_id=owner.pk, slot_index=3)
            self.assertEqual((row.instance_id, row.item_amount, row.expires_world_minute), (identity, 7, 900))
            for name, value in attrs.items():
                self.assertEqual(getattr(row, name), value)
            self.assertEqual(row.remaining_uses, -1)
            self.assertEqual([s.remaining_uses for s in Model.objects.filter(bag_id=owner.pk, slot_index__gte=4)], [0, None, -2, 8])
        stock = VillageStoreInventorySlot.objects.get(pk=stock_pk)
        self.assertEqual((stock.reserved_amount, stock.source_slot_instance_id, stock.remaining_uses), (2, identity, 0))
        self.assertEqual(stock.common_attributes, {"extra": {"keep": True}})
        self.assertEqual(stock.item_attributes, dict(attrs, extra={"keep": True}))
        self.assertEqual(MarketListing.objects.get(pk=listing_pk).dish_metadata, attrs["dish_metadata"])
        apps = self.migrate(BEFORE)
        for name, owner in (("InventorySlot", bag), ("NpcGlobalInventorySlot", npc_bag)):
            row = apps.get_model("inventory", name).objects.get(bag_id=owner.pk, slot_index=3)
            for key, value in attrs.items():
                self.assertEqual(getattr(row, key), value)
            self.assertEqual((row.instance_id, row.remaining_uses, row.item_amount), (identity, -1, 7))
        row = apps.get_model("village", "VillageStoreInventorySlot").objects.get(pk=stock_pk)
        self.assertEqual(row.item_attributes, dict(attrs, extra={"keep": True}))
        self.assertEqual(row.remaining_uses, 0)
        self.migrate(AFTER)
        self.assertEqual(MarketListing.objects.get(pk=listing_pk).remaining_uses, 8)
