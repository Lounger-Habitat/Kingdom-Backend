from copy import deepcopy
import uuid

from django.test import TestCase
from items.models import Item, ItemType
from village.agent_feedback import add_item_names


class ItemNameFeedbackTests(TestCase):
    def test_nested_batches_recipes_errors_and_market_rows_use_real_names(self):
        Item.objects.update_or_create(item_id=810001, defaults={"item_name": "测试小麦", "item_type": ItemType.INGREDIENT})
        Item.objects.update_or_create(item_id=810002, defaults={"item_name": "测试炖肉", "item_type": ItemType.FOOD})
        instance = str(uuid.uuid4())
        result = {"dish": {"item_id": 810002, "name": "测试炖肉", "amount": 2,
                           "batches": [{"instance_id": instance, "amount": 2}]},
                  "recipe": {"name": "测试配方", "output_item_id": 810002},
                  "tool_error": {"required_item_id": 810001},
                  "market": {"itemId": 810001},
                  "missing": {"item_id": 899999},
                  "snapshot": {"item_id": 810001, "item_name": "当时的小麦名称"}}
        add_item_names(result)
        batch = result["dish"]["batches"][0]
        self.assertEqual((batch["item_id"], batch["item_name"], batch["food_instance_id"]), (810002, "测试炖肉", instance))
        self.assertEqual(result["recipe"]["output_item_name"], "测试炖肉")
        self.assertEqual(result["recipe"]["name"], "测试配方")
        self.assertEqual(result["tool_error"]["required_item_name"], "测试小麦")
        self.assertEqual(result["market"]["itemName"], "测试小麦")
        self.assertNotIn("item_name", result["missing"])
        self.assertEqual(result["snapshot"]["item_name"], "当时的小麦名称")
        once = deepcopy(result)
        self.assertEqual(add_item_names(result), once)

