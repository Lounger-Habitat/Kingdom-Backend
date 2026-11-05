"""Food actions return their own state without querying every village panel."""
import copy
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from account.models import GameAccount
from inventory.models import InventoryBag
from items.models import Item, ItemType
from player.models import EquipmentDefinition, EquipmentLevel, PlayerGrowthState, PlayerSkillProgress, PlayerTutorialState
from player.progression import ensure_growth
from player.lucy_tutorial import LUCY_STEPS
from player.tutorial_catalog import VERSION
from restaurant.models import MenuItem, PlayerRestaurantState, RestaurantLevelConfig
from restaurant.player_stall import business_overview
from village.models import VillagePlayerActionReceipt, VillagePlayerDishReview
from village.player_actions import ActionError, execute_player_action
from village.tests.test_player_actions import PlayerFixture


class PlayerFoodSettlementTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        PlayerRestaurantState.objects.create(player=self.a, stall_opened_world_minute=100)

    def food(self, amount=3):
        return self.slot(amount, item_id=29201, quality=4, dish_quality=4, dish_metadata={})

    def settled(self, action, params, section):
        location = "RECORD_HALL" if section == "reviews" else "PLAYER_STALL"
        payload = self.payload(action, params, location=location)
        with CaptureQueriesContext(connection) as queries:
            reply = execute_player_action(payload)
        self.assertTrue(reply["success"], reply)
        self.assertLess(len(queries), 120, "Food actions must not rebuild all village panels")
        self.assertTrue(reply["data"]["overview_partial"])
        self.assertEqual(reply["data"]["settlement_section"], section)
        self.assertNotIn("inventory_partial", reply["data"])
        state = reply["data"]["overview"]
        self.assertIn(section, state)
        if section == "business":
            self.assertTrue(reply["data"]["business_partial"])
            self.assertFalse({"summary", "trend", "history"} & state.keys())
        self.assertTrue({"inventory", "growth"}.issubset(state))
        self.assertFalse({"market", "affairs", "plants", "production", "cooking", "processing", "farm"} & state.keys())
        saved = VillagePlayerActionReceipt.objects.get(player=self.a, request_id=payload["request_id"])
        self.assertEqual(saved.response, reply)
        return reply, payload

    def test_stall_settlements_do_not_query_sales_and_full_refresh_still_has_accounts(self):
        source = self.food(1)
        with CaptureQueriesContext(connection) as queries:
            listed, _ = self.settled("list_stall_dish", {"instance_id": str(source.instance_id), "quantity": 1}, "business")
            self.settled("withdraw_stall_dish", {"menu_id": listed["data"]["result"]["menu_id"]}, "business")
        self.assertFalse([q for q in queries if "restaurant_customertransaction" in q["sql"].lower()])
        full = self.run_action("overview", {"section": "business"})
        self.assertTrue(full["success"], full)
        self.assertFalse(full["data"].get("business_partial"))
        self.assertTrue({"summary", "trend", "history"}.issubset(full["data"]["overview"]["business"]))

    def test_listing_reuses_current_level_bonuses_without_applying_future_upgrades(self):
        from items.quality import quality_price
        PlayerRestaurantState.objects.filter(player=self.a).update(current_level=2)
        for level, bonus in ((1, 10), (2, 20), (3, 90)):
            RestaurantLevelConfig.objects.update_or_create(level=level, defaults={"reward_price_bonus_pct": bonus})
        source = self.food(1)
        reply, _ = self.settled("list_stall_dish", {"instance_id": str(source.instance_id), "quantity": 1}, "business")
        item = Item.objects.get(pk=source.item_id)
        self.assertEqual(reply["data"]["result"]["unit_price"], int(quality_price(item.item_price, 4, dish=True) * 130 / 100))

    def test_late_failure_rolls_back_inventory_rewards_and_shared_growth_state(self):
        PlayerSkillProgress.objects.update_or_create(player=self.a, skill_code="gathering", defaults={"total_xp": 80})
        growth = ensure_growth(self.a)
        before_revision = growth.revision
        source = self.food(1)
        payload = self.payload("list_stall_dish", {"instance_id": str(source.instance_id), "quantity": 1}, location="PLAYER_STALL")
        with patch("player.tutorials.observe_action", side_effect=ActionError("TEST_FAILURE", "rollback test")):
            reply = execute_player_action(payload)
        self.assertEqual(reply["result_code"], "TEST_FAILURE")
        self.bag_a.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual((self.bag_a.money, source.item_amount), (100, 1))
        self.assertFalse(MenuItem.objects.filter(player=self.a).exists())
        self.assertEqual(PlayerGrowthState.objects.get(player=self.a).revision, before_revision)
        self.assertEqual(reply["data"]["overview"]["growth"]["revision"], before_revision)
        self.assertEqual(reply["data"]["overview"]["money"], 100)

    def test_food_actions_do_not_scan_world_events_expirations_or_unchanged_tutorials(self):
        source = self.food()
        forbidden = AssertionError("Unrelated world or onboarding work is outside food settlement")
        with patch("village.personal_commissions.expire_commissions", side_effect=forbidden), \
             patch("village.starter_supply.expire_supply", side_effect=forbidden), \
             patch("village.player_farm.maintain_leases", side_effect=forbidden), \
             patch("village.daily_events.evaluate_daily_events", side_effect=forbidden), \
             patch("player.starter.starter_overview", side_effect=forbidden), \
             patch("player.tutorials.tutorial_overview", side_effect=forbidden):
            self.settled("request_dish_review", {"instance_id": str(source.instance_id)}, "reviews")
            listed, _ = self.settled("list_stall_dish", {"instance_id": str(source.instance_id), "quantity": 1}, "business")
            self.settled("withdraw_stall_dish", {"menu_id": listed["data"]["result"]["menu_id"]}, "business")

    def test_changed_business_tutorial_is_still_returned_with_the_settlement(self):
        completed = {code: "fixture" for code in LUCY_STEPS}
        completed.update(business_stock="fixture", business_open="fixture")
        tutorial = PlayerTutorialState.objects.create(player=self.a, version=VERSION, started=True,
            route_code="business", completed=completed)
        source = self.food(1)
        reply = self.run_action("list_stall_dish", {"instance_id": str(source.instance_id), "quantity": 1}, location="PLAYER_STALL")
        self.assertTrue(reply["success"], reply)
        tutorial.refresh_from_db()
        self.assertIn("business_list", tutorial.completed)
        self.assertEqual(reply["data"]["overview"]["tutorial"]["current_task"]["code"], "business_sale")

    def test_new_introductory_reward_still_refreshes_money_and_starter_once(self):
        PlayerSkillProgress.objects.update_or_create(player=self.a, skill_code="gathering", defaults={"total_xp": 80})
        source = self.food(1)
        payload = self.payload("list_stall_dish", {"instance_id": str(source.instance_id), "quantity": 1}, location="PLAYER_STALL")
        reply = execute_player_action(payload)
        self.assertTrue(reply["success"], reply)
        self.assertEqual(reply["data"]["overview"]["money"], 120)
        self.assertIn("starter", reply["data"]["overview"])
        self.assertIn("tutorial", reply["data"]["overview"])
        self.assertTrue(execute_player_action(payload)["replayed"])
        self.bag_a.refresh_from_db()
        self.assertEqual(self.bag_a.money, 120)

    def test_food_inventory_queries_do_not_scale_with_tools_and_keep_real_durability(self):
        item = Item.objects.create(item_id=989981, item_name="分区查询测试工具", item_type=ItemType.COLLECT_TOOL,
            item_price=10, max_use_count=99)
        definition = EquipmentDefinition.objects.create(item=item, category="gathering", required_skill="gathering")
        EquipmentLevel.objects.create(equipment=definition, level=1, max_durability=20)
        self.slot(1, item_id=item.pk, remaining_uses=20)
        baseline = {}
        for section in ("reviews", "business"):
            payload = self.payload("overview", {"section": section})
            payload["read_only"] = True
            with CaptureQueriesContext(connection) as queries:
                reply = execute_player_action(payload)
            self.assertTrue(reply["success"], reply)
            baseline[section] = len(queries)
        for _ in range(15):
            self.slot(1, item_id=item.pk, remaining_uses=20)
        for section in ("reviews", "business"):
            with self.subTest(section=section):
                payload = self.payload("overview", {"section": section})
                payload["read_only"] = True
                with CaptureQueriesContext(connection) as queries:
                    reply = execute_player_action(payload)
                self.assertLessEqual(len(queries), baseline[section] + 1)
                self.assertEqual(len(reply["data"]["overview"]["inventory"]), 16)
                for row in reply["data"]["overview"]["inventory"]:
                    self.assertEqual(row["attributes"]["usage"]["maximum"], 20)
                    self.assertTrue(row["general_store_sale"]["allowed"])

    def test_sample_submit_cancel_claim_preserve_assets_and_replay(self):
        source = self.food()
        submitted, payload = self.settled("request_dish_review", {"instance_id": str(source.instance_id)}, "reviews")
        review = submitted["data"]["result"]["review"]
        self.assertEqual(submitted["data"]["overview"]["reviews"]["entries"][0]["review_id"], review["review_id"])
        self.assertEqual(execute_player_action(copy.deepcopy(payload)), dict(submitted, replayed=True))
        self.assertEqual(VillagePlayerDishReview.objects.filter(player=self.a).count(), 1)
        self.settled("cancel_dish_review", {"review_id": review["review_id"]}, "reviews")
        claimed, _ = self.settled("claim_dish_review", {"review_id": review["review_id"]}, "reviews")
        self.assertEqual(sum(row["quantity"] for row in claimed["data"]["overview"]["inventory"]), 3)
        self.assertEqual(claimed["data"]["overview"]["reviews"]["entries"][0]["can_claim"], False)

    def test_listing_withdrawal_replace_the_complete_inventory(self):
        source = self.food(1)
        listed, _ = self.settled("list_stall_dish", {"instance_id": str(source.instance_id), "quantity": 1}, "business")
        self.assertEqual(listed["data"]["overview"]["inventory"], [])
        self.assertEqual(listed["data"]["overview"]["business"]["occupied"], 1)
        withdrawn, payload = self.settled("withdraw_stall_dish", {"menu_id": listed["data"]["result"]["menu_id"]}, "business")
        self.assertEqual(withdrawn["data"]["overview"]["business"]["menu"], [])
        self.assertEqual(sum(row["quantity"] for row in withdrawn["data"]["overview"]["inventory"]), 1)
        self.assertEqual(execute_player_action(payload), dict(withdrawn, replayed=True))
        self.assertFalse(MenuItem.objects.filter(player=self.a).exists())

    def test_rejected_food_actions_return_current_scoped_state_without_asset_changes(self):
        source = self.food(1)
        listed = execute_player_action(self.payload("list_stall_dish",
            {"instance_id": str(source.instance_id), "quantity": 1}, location="PLAYER_STALL"))
        for action, params, section, code in (
            ("list_stall_dish", {"instance_id": str(source.instance_id), "quantity": 1}, "business", "ITEM_NOT_OWNED"),
            ("request_dish_review", {"instance_id": str(source.instance_id)}, "reviews", "DISH_REVIEW_SAMPLE_UNAVAILABLE"),
        ):
            with self.subTest(action=action):
                reply = execute_player_action(self.payload(action, params, location="PLAYER_STALL" if section == "business" else "RECORD_HALL"))
                self.assertEqual(reply["result_code"], code)
                self.assertEqual(reply["data"]["settlement_section"], section)
                self.assertIn(section, reply["data"]["overview"])
                self.assertEqual(reply["data"]["overview"]["inventory"], [])
        self.assertTrue(MenuItem.objects.filter(pk=listed["data"]["result"]["menu_id"]).exists())

    def test_section_refreshes_are_private_read_only_and_do_not_create_assets(self):
        self.food()
        observer = GameAccount.objects.create(username="food-section-observer")
        for section in ("reviews", "business"):
            for player in (self.a, observer):
                with self.subTest(section=section, player=player.pk):
                    payload = self.payload("overview", {"section": section}, player=player)
                    payload["read_only"] = True
                    with CaptureQueriesContext(connection) as queries:
                        reply = execute_player_action(payload)
                    self.assertTrue(reply["success"], reply)
                    self.assertTrue(reply["data"]["overview_partial"])
                    self.assertEqual(reply["data"]["overview_section"], section)
                    self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
                    if player == observer:
                        self.assertEqual(reply["data"]["overview"]["inventory"], [])
        self.assertFalse(InventoryBag.objects.filter(player=observer).exists())
        self.assertFalse(PlayerRestaurantState.objects.filter(player=observer).exists())

    def test_stall_queries_do_not_grow_per_food_batch_or_load_saved_panel_overviews(self):
        self.food()
        VillagePlayerActionReceipt.objects.create(player=self.a, request_id=self.payload("overview")["request_id"],
            action="buy_store", payload_hash="x" * 64, world_minute=100,
            response={"success": True, "data": {"result": {"purchased_batches": []}, "overview": {"padding": "x" * 100000}}})
        with CaptureQueriesContext(connection) as one:
            business_overview(self.a, self.bag_a, 100)
        for _ in range(15):
            self.food()
        with CaptureQueriesContext(connection) as many:
            state = business_overview(self.a, self.bag_a, 100)
        self.assertEqual(len(state["inventory"]), 16)
        self.assertLessEqual(len(many), len(one) + 1)
        receipt_queries = [q["sql"] for q in many if "village_villageplayeractionreceipt" in q["sql"].lower()]
        self.assertEqual(len(receipt_queries), 1)
        self.assertNotRegex(receipt_queries[0],
            r'(?:SELECT |, )"village_villageplayeractionreceipt"\."response"(?:, | FROM )')
