import uuid

from django.test import TestCase

from account.models import GameAccount
from inventory.models import InventoryBag, InventorySlot
from inventory.storage import add_inventory_items, add_store_items, attributes_from_slot
from items.models import Item, ItemType
from village.models import VillageStore


class InventoryStackStorageTests(TestCase):
    def setUp(self):
        player = GameAccount.objects.create(username="stack-storage", password="test")
        self.bag = InventoryBag.objects.create(player=player, character_id=player.username)
        self.item = Item.objects.create(item_id=990101, item_name="测试材料", quality_enabled=True)

    def test_material_merges_and_reports_delta(self):
        first = add_inventory_items(self.bag, self.item.pk, 3)[0][0]
        rows = add_inventory_items(self.bag, self.item.pk, 2)
        self.assertEqual([(s.instance_id, n) for s, n in rows], [(first.instance_id, 2)])
        first.refresh_from_db()
        self.assertEqual(first.item_amount, 5)

    def test_quality_and_owner_are_separate(self):
        add_inventory_items(self.bag, self.item.pk, 3, attributes={"quality": 3})
        add_inventory_items(self.bag, self.item.pk, 2, attributes={"quality": 4})
        other = InventoryBag.objects.create(player=self.bag.player, character_id="escrow")
        add_inventory_items(other, self.item.pk, 1)
        self.assertEqual(self.bag.slots.filter(item_amount__gt=0).count(), 2)
        self.assertEqual(other.slots.get().item_amount, 1)

    def test_stack_limit_fills_existing_then_creates(self):
        self.item.max_stack_size = 4
        self.item.save()
        first = add_inventory_items(self.bag, self.item.pk, 3)[0][0]
        rows = add_inventory_items(self.bag, self.item.pk, 7)
        self.assertEqual([n for _, n in rows], [1, 4, 2])
        self.assertEqual(rows[0][0].instance_id, first.instance_id)
        self.assertEqual(list(self.bag.slots.values_list("item_amount", flat=True)), [4, 4, 2])

    def test_dishes_are_individual_and_keep_metadata(self):
        self.item.item_type = ItemType.FOOD
        self.item.save()
        rid = uuid.uuid4()
        rows = add_inventory_items(self.bag, self.item.pk, 3,
            attributes=dict(dish_metadata={"recipe_id": 12}, source_request_id=rid,
                produced_world_minute=90, ingredient="测试材料", quality=4))
        self.assertEqual([s.item_amount for s, _ in rows], [1, 1, 1])
        self.assertEqual(len({s.item_bag_name for s, _ in rows}), 3)
        self.assertTrue(all(s.source_request_id == rid and s.dish_metadata == {"recipe_id": 12} for s, _ in rows))

    def test_tools_keep_individual_usage_and_level(self):
        self.item.item_type = ItemType.COLLECT_TOOL
        self.item.save()
        rows = add_inventory_items(self.bag, self.item.pk, 2, attributes=dict(remaining_uses=8, equipment_level=3))
        self.assertEqual([s.item_amount for s, _ in rows], [1, 1])
        self.assertTrue(all(s.remaining_uses == 8 and s.equipment_level == 3 for s, _ in rows))

    def test_old_origin_does_not_prevent_material_merge(self):
        old = InventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=self.item.pk,
            item_amount=2, produced_world_minute=10, source_request_id=uuid.uuid4())
        output = add_inventory_items(self.bag, self.item.pk, 1,
            attributes=dict(produced_world_minute=20, source_request_id=uuid.uuid4()))[0][0]
        self.assertEqual(output.instance_id, old.instance_id)
        output.refresh_from_db()
        self.assertEqual(output.item_amount, 3)
        self.assertIsNone(output.produced_world_minute)
        self.assertIsNone(output.source_request_id)

    def test_store_merges_and_retains_reserved_quantity(self):
        store = VillageStore.objects.create(store_code="STACK_TEST", location_code="TEST", display_name="测试店")
        first = add_store_items(store, self.item.pk, 3)[0][0]
        first.reserved_amount = 1
        first.save(update_fields=["reserved_amount"])
        output, delta = add_store_items(store, self.item.pk, 2)[0]
        self.assertEqual((output.instance_id, output.item_amount, output.reserved_amount, delta),
            (first.instance_id, 5, 1, 2))

    def test_dish_roundtrip_preserves_production_evidence(self):
        self.item.item_type = ItemType.FOOD
        self.item.save()
        rid = uuid.uuid4()
        source = add_inventory_items(self.bag, self.item.pk, 1,
            attributes=dict(source_request_id=rid, produced_world_minute=90,
                dish_metadata={"maker_player_id": "7"}))[0][0]
        store = VillageStore.objects.create(store_code="DISH_STACK_TEST", location_code="TEST", display_name="测试店")
        stored = add_store_items(store, self.item.pk, 1, attributes=attributes_from_slot(source))[0][0]
        other = InventoryBag.objects.create(player=self.bag.player, character_id="return")
        returned = add_inventory_items(other, self.item.pk, 1, attributes=attributes_from_slot(stored))[0][0]
        self.assertEqual(str(returned.source_request_id), str(rid))
        self.assertEqual(returned.produced_world_minute, 90)
        self.assertEqual(returned.dish_metadata, source.dish_metadata)
