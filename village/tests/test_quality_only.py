"""Quality economy regressions, including old inventory and exactly-once settlement."""
import copy
import random
import uuid
from collections import Counter
from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import SimpleTestCase, TestCase

from inventory.batches import project_batch
from inventory.models import InventorySlot
from items.models import Item, ItemType
from items.quality import (PRODUCTION_WEIGHTS, cooking_quality, ingredient_average,
                           production_quality, quality_price)
from player.models import PlayerState, SkillRewardReceipt
from village.lucy_catalog import install_catalog
from village.models import (VillageAccount, VillagePlayerActionReceipt, VillageStore,
                            VillageStoreAccount, VillageStoreInventorySlot, VillageStoreListing)
from village.player_actions import execute_player_action
from village.player_cooking import is_player_cooked, preview_cooking
from village.player_store import store_sale_quote
from village.tests.test_player_actions import PlayerFixture


class QualityMathTests(SimpleTestCase):
    def test_production_distribution_and_all_six_sources(self):
        for level, weights in PRODUCTION_WEIGHTS.items():
            self.assertEqual(sum(weights), 100)
            counts = Counter(production_quality(level, random.Random(seed)) for seed in range(12000))
            for grade, expected in enumerate(weights, 1):
                self.assertAlmostEqual(counts[grade] / 120, expected, delta=1.5)
        self.assertEqual(set(counts), set(range(1, 7)))
        self.assertGreater(counts[1], 0)

    def test_skill_and_tools_improve_distribution_without_lowering_any_sample(self):
        for seed in range(1500):
            outcomes = [production_quality(level, random.Random(seed)) for level in (1, 4, 7, 10)]
            self.assertEqual(outcomes, sorted(outcomes))
            base = production_quality(7, random.Random(seed))
            improved = production_quality(7, random.Random(seed), .2)
            self.assertTrue(base <= improved <= min(6, base + 1))

    def test_good_ingredients_stochastically_improve_cooking(self):
        for seed in range(1000):
            results = [cooking_quality(3, grade, 4, .1, random.Random(seed)) for grade in range(1, 7)]
            self.assertEqual(results, sorted(results))
            self.assertTrue(all(1 <= grade <= 6 for grade in results))

    def test_average_uses_selected_amounts_and_excludes_seasoning(self):
        rows = [dict(item_id=1056, quality=5, quantity=100, selected_quantity=2),
                dict(item_id=1040, quality=4, quantity=100, selected_quantity=1),
                dict(item_id=1049, quality=1, quantity=100)]
        self.assertAlmostEqual(ingredient_average(rows), 14 / 3)

    def test_both_price_scales_are_monotone_and_round_consistently(self):
        self.assertEqual([quality_price(10, q) for q in range(1, 7)], [6, 8, 10, 12, 15, 18])
        self.assertEqual([quality_price(10, q, dish=True) for q in range(1, 7)], [3, 7, 10, 15, 20, 30])
        self.assertEqual(quality_price(1, 1), 1)
        for invalid in (0, 7, True, None):
            with self.assertRaises(ValueError):
                quality_price(10, invalid)


class QualityCookingTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        for item_id in (1049, 1056, 1009, 1034, 1057, 1077, 1025, 1042, 1023):
            Item.objects.update_or_create(pk=item_id, defaults=dict(item_name=str(item_id),
                item_type=ItemType.SEASONING if item_id == 1049 else ItemType.INGREDIENT))
        install_catalog()
        PlayerState.objects.create(player_id=str(self.a.pk), action_points=200)
        self.tool = InventorySlot.objects.create(bag=self.bag_a, slot_index=0, item_id=4106,
            item_amount=1, remaining_uses=20)

    def cook(self, source):
        params = dict(recipe_id=29201, batches=1, tool_instance_id=str(self.tool.instance_id),
            selections=[dict(instance_id=str(source.instance_id), quantity=2)])
        quote = preview_cooking(self.a, self.bag_a, params, 100)
        self.assertTrue(quote["available"], quote)
        payload = self.payload("cook_dish", dict(params, expected_quote=quote["quote_hash"]), location="RESTAURANT")
        answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer.get("result_code"))
        return answer, payload

    def test_old_unknown_expired_low_quality_food_cooks_and_awards_once(self):
        source = self.slot(4, quality=1, expires_world_minute=50, freshness_unknown=True)
        with patch("village.player_cooking.calculate_quality", return_value=2):
            answer, payload = self.cook(source)
        dish = answer["data"]["result"]["dish"]
        self.assertEqual(dish["quality"], 1)
        self.assertIsNone(dish["expires_world_minute"])
        self.assertFalse(dish["freshness_unknown"])
        self.assertEqual(answer["data"]["result"]["growth_rewards"][0]["xp_gained"], 40)
        self.assertEqual(execute_player_action(copy.deepcopy(payload)), answer)
        source.refresh_from_db()
        self.tool.refresh_from_db()
        self.assertEqual((source.item_amount, self.tool.remaining_uses), (2, 19))
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a, source_type="player_cooking").count(), 1)
        self.assertTrue(is_player_cooked(self.bag_a.slots.get(instance_id=dish["instance_id"]), self.a))

    def test_projection_hides_expiry_without_changing_stored_audit_fields(self):
        source = self.slot(2, quality=2, expires_world_minute=1, freshness_unknown=True)
        projected = project_batch(source, Item.objects.get(pk=1030), 100)
        self.assertEqual(projected["quality"], 2)
        self.assertIsNone(projected["freshness"])
        source.refresh_from_db()
        self.assertEqual((source.quality, source.expires_world_minute, source.freshness_unknown), (2, 1, True))

    def test_legacy_v1_unknown_dish_is_still_proven_as_own_work(self):
        answer, payload = self.cook(self.slot(4))
        source = self.bag_a.slots.get(instance_id=answer["data"]["result"]["dish"]["instance_id"])
        source.freshness_unknown = True
        source.dish_metadata = dict(source.dish_metadata, rule_version="player-cooking-v1")
        source.save()
        receipt = VillagePlayerActionReceipt.objects.get(request_id=payload["request_id"])
        for key in ("dish",):
            receipt.response["data"]["result"][key]["freshness_unknown"] = True
            receipt.response["data"]["result"][key]["dish_metadata"] = source.dish_metadata
        for dish in receipt.response["data"]["result"].get("dishes", []):
            dish["freshness_unknown"] = True
            dish["dish_metadata"] = source.dish_metadata
        receipt.save()
        self.assertTrue(is_player_cooked(source, self.a))


class QualityStoreTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.store, _ = VillageStore.objects.update_or_create(pk="GENERAL_STORE", defaults=dict(
            display_name="杂货店", location_code="GENERAL_STORE", operator_npc=self.npc, status="OPEN"))
        self.account, _ = VillageStoreAccount.objects.update_or_create(store=self.store, defaults=dict(balance=1000))
        VillageAccount.objects.update_or_create(account_code="PUBLIC_FUND", defaults=dict(balance=0))
        Item.objects.filter(pk=1030).update(item_price=10)

    def listing(self, **kwargs):
        return VillageStoreListing.objects.create(store=self.store, item_id=1030, unit_price=10,
            listed_by_npc=self.npc, created_request_id=uuid.uuid4(), listed_world_minute=90, **kwargs)

    def test_legacy_commodity_ingredient_migration_enables_display_and_quality_price(self):
        Item.objects.filter(pk=1030).update(item_type=ItemType.COMMODITY, quality_enabled=False)
        source = self.slot(2, quality=5)
        import_module("village.migrations.0087_legacy_ingredient_quality").enable_legacy_quality(apps, connection.schema_editor())
        item = Item.objects.get(pk=1030)
        self.assertEqual(item.item_type, ItemType.COMMODITY)
        self.assertTrue(item.quality_enabled)
        self.assertEqual(project_batch(source, item, 100)["quality"], 5)
        self.assertEqual(store_sale_quote(source, item, 100)["unit_price"], 15)

    def test_buyback_prices_old_batches_by_quality_and_replay_conserves_assets(self):
        source = self.slot(3, quality=5, expires_world_minute=1, freshness_unknown=True)
        quote = store_sale_quote(source, Item.objects.get(pk=1030), 100)
        self.assertEqual((quote["allowed"], quote["unit_price"]), (True, 15))
        payload = self.payload("sell_store", dict(instance_id=str(source.instance_id), quantity=2,
            expected_unit_price=15), location="GENERAL_STORE")
        answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer.get("result_code"))
        self.assertEqual(execute_player_action(payload), answer)
        self.bag_a.refresh_from_db()
        self.account.refresh_from_db()
        bonus = sum(row["earned_gold"] for row in answer["data"]["result"].get("starter_rewards", []))
        self.assertEqual((self.bag_a.money, self.account.balance), (130 + bonus, 970))
        moved = VillageStoreInventorySlot.objects.get(store=self.store, source_slot_instance_id=source.instance_id)
        self.assertEqual((moved.item_amount, moved.quality, moved.freshness_unknown if hasattr(moved, "freshness_unknown") else moved.common_attributes["freshness_unknown"]), (2, 5, True))

    def test_shelves_split_quality_and_purchase_never_substitutes_a_different_grade(self):
        listing = self.listing()
        low = VillageStoreInventorySlot.objects.create(store=self.store, listing=listing, item_id=1030,
            item_amount=2, quality=2)
        high = VillageStoreInventorySlot.objects.create(store=self.store, listing=listing, item_id=1030,
            item_amount=2, quality=5)
        self.assertNotEqual(low.listing_id, high.listing_id)
        from village.phase3_catalog import BASE_PRICES
        base = BASE_PRICES.get(1030, 10)
        self.assertEqual((low.listing.unit_price, high.listing.unit_price), (quality_price(base, 2), quality_price(base, 5)))
        params = dict(listing_id=str(high.listing_id), quantity=1, expected_quality=5, expected_unit_price=high.listing.unit_price)
        answer = self.run_action("buy_store", params, location="GENERAL_STORE")
        self.assertTrue(answer["success"], answer.get("result_code"))
        got = answer["data"]["result"]["purchased_batches"]
        self.assertEqual([(row["quality"], row["quantity"]) for row in got], [(5, 1)])
        low.refresh_from_db()
        self.assertEqual(low.item_amount, 2)
        bad = self.run_action("buy_store", dict(params, expected_quality=2), location="GENERAL_STORE")
        self.assertEqual(bad["result_code"], "QUALITY_CHANGED")

    def test_quality_migration_preserves_quantities_prices_and_batch_ids(self):
        listing = self.listing(price_mode="CUSTOM")
        first = VillageStoreInventorySlot.objects.create(store=self.store, listing=listing, item_id=1030,
            item_amount=2, quality=2)
        second = VillageStoreInventorySlot.objects.create(store=self.store, listing=listing, item_id=1030,
            item_amount=3, quality=5)
        VillageStoreInventorySlot.objects.filter(pk=second.pk).update(listing=listing)
        ids = {first.instance_id, second.instance_id}
        import_module("village.migrations.0086_store_quality_only").split_shelves(apps, connection.schema_editor())
        rows = list(VillageStoreInventorySlot.objects.filter(instance_id__in=ids).select_related("listing"))
        self.assertEqual(sum(row.item_amount for row in rows), 5)
        self.assertEqual({row.instance_id for row in rows}, ids)
        self.assertTrue(all(row.quality == row.listing.quality and row.listing.unit_price == 10 for row in rows))
