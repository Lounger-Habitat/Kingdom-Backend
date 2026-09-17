from django.test import TestCase
from items.models import Item, ItemType
from marketplace.models import MarketListing
from marketplace.serializers import serialize_listing, serialize_listings


class PublicTooltipTests(TestCase):
    def test_market_uses_components_and_separates_listing_expiry(self):
        item = Item.objects.create(item_id=49001, item_name="测试菜品", item_type=ItemType.FOOD)
        listing = MarketListing.objects.create(
            source_type="system", item_id=item.item_id, price=90, total_amount=3, remaining_amount=2,
            item_snapshot={"cookTime": 999, "ingredient": "旧食材", "expiresWorldMinute": 800,
                           "sourceRequestId": "private", "batchMetadataVersion": 1},
            ingredient="玉米×2", cook_time=42, dish_quality=4,
            rated=True, rating_price=88, overall_score=77, review_comment="好吃",
            dish_metadata={"private": "secret"},
        )
        data = serialize_listing(listing)
        batch = data["batch"]
        self.assertEqual(batch["dish"]["cookTime"], 42)
        self.assertEqual(batch["dish"]["ingredients"], "玉米×2")
        self.assertEqual(batch["dish"]["review"], {"rated": True, "price": 88, "score": 77, "comment": "好吃"})
        self.assertEqual(batch["quantity"], 2)
        self.assertEqual(batch["quality"], 4)
        self.assertEqual(batch["freshness"]["expiresWorldMinute"], 800)
        self.assertIsNone(data["expiresAt"])
        self.assertEqual(batch["id"], "")
        self.assertIsNone(batch["origin"])
        self.assertEqual(batch["dish"]["metadata"], {})
        self.assertEqual(data["price"], 90)

    def test_market_null_usage_is_unknown_and_context_is_bulk_loaded(self):
        item = Item.objects.create(item_id=49002, item_name="测试工具", item_type=ItemType.HOE_TOOL, max_use_count=20)
        listings = [MarketListing.objects.create(source_type="system", item_id=item.item_id, price=10) for _ in range(3)]
        # Materialize relation-bearing instances before counting catalog/clock reads.
        listings = list(MarketListing.objects.filter(pk__in=[row.pk for row in listings]))
        with self.assertNumQueries(2):
            data = serialize_listings(listings)
        self.assertEqual(data[0]["batch"]["usage"], {"mode": "UNKNOWN", "remaining": None, "maximum": 20})
        self.assertIsNone(data[0]["batch"]["quality"])
        self.assertIsNone(data[0]["batch"]["dish"])
