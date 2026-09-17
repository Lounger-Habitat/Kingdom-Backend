import copy
import uuid
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext

from inventory.batches import project_batch
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from inventory.serializers import InventorySlotSerializer, serialize_npc_global_bag
from items.models import Item, ItemType
from items.serializers import ItemSerializer


def slot(**changes):
    values = dict(instance_id=uuid.uuid4(), item_id=1030, item_amount=5,
                  dish_quality=3, remaining_uses=None, rated=False, rating_price=-1,
                  overall_score=0, ingredient="", cook_time=0, review_comment="",
                  dish_metadata={}, produced_world_minute=600,
                  expires_world_minute=None, freshness_unknown=False, source_request_id=uuid.uuid4())
    return SimpleNamespace(**(values | changes))


class BatchContractTests(SimpleTestCase):
    def item(self, **kw):
        return Item(item_id=1030, item_name="玉米", item_type=ItemType.VEGETABLE, **kw)

    def test_ordinary_crop_has_no_dish_usage_or_implicit_quality(self):
        source = slot()
        before = copy.deepcopy(source.__dict__)
        result = project_batch(source, self.item(), 700)
        self.assertEqual(result["id"], str(source.instance_id))
        self.assertEqual(result["quantity"], 5)
        self.assertTrue(result["stacking"]["stackable"])
        for key in ("dish", "usage", "quality", "freshness"):
            self.assertIsNone(result[key])
        self.assertEqual(source.__dict__, before)

    def test_quality_is_explicitly_enabled_and_limit_is_configuration(self):
        result = project_batch(slot(dish_quality=5), self.item(quality_enabled=True, max_stack_size=20), 700)
        self.assertEqual(result["quality"], 5)
        self.assertEqual(result["stacking"], {"stackable": True, "maxPerSlot": 20})

    def test_dish_keeps_quality_recipe_review_and_batch_identity(self):
        food = Item(item_id=29001, item_type=ItemType.FOOD)
        source = slot(item_id=29001, dish_quality=6, rated=True, rating_price=90,
                      ingredient="玉米×2", dish_metadata={"recipe_id": 5, "maker_npc_id": "10014"})
        result = project_batch(source, food, 700)
        self.assertFalse(result["stacking"]["stackable"])
        self.assertEqual(result["quality"], 6)
        self.assertEqual(result["dish"]["metadata"], source.dish_metadata)
        self.assertEqual(result["dish"]["review"]["price"], 90)

    def test_catalog_reclassification_does_not_merge_special_instances(self):
        for fields in ({"dish_metadata": {"recipe_id": 1}}, {"rated": True}, {"remaining_uses": 5}):
            with self.subTest(fields=fields):
                self.assertFalse(project_batch(slot(**fields), self.item(), 700)["stacking"]["stackable"])

    def test_tool_modes_are_distinct_even_without_durability_data(self):
        tool = Item(item_id=4001, item_type=ItemType.HOE_TOOL, max_use_count=20)
        for remaining, mode in ((None, "UNKNOWN"), (-1, "UNLIMITED"), (0, "FINITE"), (7, "FINITE"), (-2, "INVALID")):
            with self.subTest(remaining=remaining):
                result = project_batch(slot(remaining_uses=remaining), tool, 700)
                self.assertFalse(result["stacking"]["stackable"])
                self.assertEqual(result["usage"]["mode"], mode)
                self.assertEqual(result["usage"]["remaining"], remaining)

    def test_freshness_boundary_and_unknown_do_not_get_reported_as_fresh(self):
        for expiry, minute, unknown, expected in ((701, 700, False, "FRESH"), (700, 700, False, "EXPIRED"),
                                                (600, 700, False, "EXPIRED"), (900, 700, True, "UNKNOWN"),
                                                (900, None, False, "UNKNOWN")):
            result = project_batch(slot(expires_world_minute=expiry, freshness_unknown=unknown), self.item(), minute)
            self.assertEqual(result["freshness"]["state"], expected)

    def test_missing_catalog_and_disabled_stacking_fail_closed(self):
        for item in (None, self.item(stackable=False)):
            self.assertFalse(project_batch(slot(), item, 700)["stacking"]["stackable"])

    def test_catalog_wire_rules_match_batch_rules(self):
        item = self.item(quality_enabled=True, max_stack_size=10)
        rules = ItemSerializer(item).data["inventoryRules"]
        batch = project_batch(slot(), item, 700)
        self.assertEqual(rules["maxPerSlot"], batch["stacking"]["maxPerSlot"])
        self.assertEqual(rules["stackable"], batch["stacking"]["stackable"])


class BatchSerializationTests(TestCase):
    def setUp(self):
        npc = NpcDefinition.objects.create(agent_id="TEST_BATCH_CONTRACT", name="Batch Test", is_test_only=True)
        self.bag = NpcGlobalInventory.objects.create(npc=npc)
        self.item = Item.objects.create(item_id=980001, item_name="测试玉米", item_type=ItemType.VEGETABLE)
        for i in range(7):
            NpcGlobalInventorySlot.objects.create(bag=self.bag, slot_index=i, item_id=self.item.pk,
                item_amount=i + 1, produced_world_minute=600 + i, source_request_id=uuid.uuid4())

    def test_observation_preserves_seven_batches_and_legacy_contract_without_writes(self):
        before = list(self.bag.slots.values())
        with CaptureQueriesContext(connection) as queries:
            data = serialize_npc_global_bag(self.bag)
        self.assertEqual(len(data["items"]), 7)
        self.assertEqual(sum(row["itemAmount"] for row in data["items"]), 28)
        self.assertEqual(sum(row["batch"]["quantity"] for row in data["items"]), 28)
        self.assertEqual(list(self.bag.slots.values()), before)
        self.assertEqual(len(queries), 3, "One query each for slots, catalog and time; no per-slot catalog queries")
        for i, row in enumerate(data["items"]):
            self.assertEqual(row["slotIndex"], i)
            self.assertEqual(row["instanceID"], row["batch"]["id"])
            self.assertIn("dishQuality", row)
            self.assertIsNone(row["batch"]["quality"])
            self.assertIsNone(row["batch"]["usage"])

    def test_single_serializer_and_unknown_tool_preserve_null_in_new_contract(self):
        self.item.item_type = ItemType.HOE_TOOL
        self.item.save()
        data = InventorySlotSerializer(self.bag.slots.first()).data
        self.assertEqual(data["remainingUses"], -1, "Legacy format stays compatible")
        self.assertEqual(data["batch"]["usage"]["mode"], "UNKNOWN")
        self.assertIsNone(data["batch"]["usage"]["remaining"])
