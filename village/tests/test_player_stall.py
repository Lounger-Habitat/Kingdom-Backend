import copy
import importlib
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from account.models import GameAccount
from inventory.components import BATCH_COPY_FIELDS
from inventory.models import InventoryBag, InventorySlot
from items.models import Item, ItemType
from marketplace.services import create_listing
from player.models import PlayerMilestoneProgress, PlayerRecipe, PlayerSkillProgress, SkillRewardReceipt
from recipes.models import Recipe
from restaurant.models import (CustomerTransaction, MenuItem, PlayerRestaurantState, RestaurantConfig,
                               RestaurantHourlySlot, RestaurantLevelConfig, RestaurantSaleEvent)
from restaurant.player_stall import business_overview, process_stall_tick
from restaurant.services import RestaurantService
from village.models import EconomicLedgerEntry, VillagePlayerActionReceipt
from village.player_actions import execute_player_action
from village.tests.test_player_actions import PlayerFixture


class PlayerStallTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.clock.apply_world_minute(600); self.clock.save()
        RestaurantConfig.objects.update_or_create(pk=1, defaults=dict(opening_hour=10, closing_hour=19,
            base_hourly_sales=3, base_menu_capacity=5, hourly_sales_per_level=1, menu_capacity_per_level=1))
        Item.objects.update_or_create(pk=1002, defaults=dict(item_name="木材", item_type=ItemType.COMMODITY))

    def at(self, minute):
        self.clock.apply_world_minute(minute); self.clock.save()

    def food(self, amount=3, bag=None, **kwargs):
        return self.slot(amount, bag=bag, item_id=29201, quality=4, source_request_id=uuid.uuid4(),
            dish_metadata=dict(maker_player_id=str(self.a.pk), production_batch_id=str(uuid.uuid4())),
            ingredient="玉米×2", cook_time=60, rated=True, rating_price=12, overall_score=82,
            review_comment="正式样品评语", **kwargs)

    def act(self, action, params=None, **kwargs):
        return self.run_action(action, params, location="PLAYER_STALL", **kwargs)

    def opened(self):
        response = self.act("open_player_stall")
        self.assertTrue(response["success"], response)
        return PlayerRestaurantState.objects.get(player=self.a)

    def listed(self, source=None, quantity=1):
        source = source or self.food()
        response = self.act("list_stall_dish", dict(instance_id=str(source.instance_id), quantity=quantity))
        self.assertTrue(response["success"], response)
        return MenuItem.objects.get(pk=response["data"]["result"]["menu_id"])

    def schedule(self, minutes=(5, 6, 7)):
        hourly, _ = RestaurantHourlySlot.objects.get_or_create(player=self.a, game_year=1, game_month=1,
            game_day=1, game_hour=10, defaults=dict(quota=len(minutes)))
        hourly.sale_events.all().delete()
        RestaurantSaleEvent.objects.bulk_create([RestaurantSaleEvent(slot=hourly, scheduled_minute=m) for m in minutes])
        return hourly

    def test_readonly_overview_does_not_create_state_bag_clock_or_growth(self):
        newcomer = GameAccount.objects.create(username="stall-observer")
        empty_bag = InventoryBag(player=newcomer, character_id=newcomer.username)
        with CaptureQueriesContext(connection) as queries:
            overview = business_overview(newcomer, empty_bag, 600, read_only=True)
            payload = self.payload("overview")
            payload["read_only"] = True
            execute_player_action(payload)
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertEqual((overview["is_open"], overview["capacity"], overview["observed_world_minute"]), (False, 5, 600))
        self.assertFalse(PlayerRestaurantState.objects.filter(player=newcomer).exists())
        self.assertFalse(InventoryBag.objects.filter(player=newcomer).exists())

    def test_list_withdraw_preserve_every_batch_field_and_receipt_replay_and_ownership(self):
        legacy = PlayerRestaurantState.objects.create(player=self.a, current_level=4)
        self.opened(); legacy.refresh_from_db()
        self.assertEqual(legacy.current_level, 4)
        source = self.food()
        original = {field: copy.deepcopy(getattr(source, field)) for field in BATCH_COPY_FIELDS}
        payload = self.payload("list_stall_dish", dict(instance_id=str(source.instance_id), quantity=2), location="PLAYER_STALL")
        listed = execute_player_action(payload)
        self.assertTrue(listed["success"], listed)
        self.assertEqual(dict(listed, replayed=True), execute_player_action(copy.deepcopy(payload)))
        menu = MenuItem.objects.get(pk=listed["data"]["result"]["menu_id"])
        self.assertNotEqual(menu.batch_slot.bag_id, self.bag_a.pk)
        self.assertEqual({f: getattr(menu.batch_slot, f) for f in BATCH_COPY_FIELDS}, original)
        foreign = self.act("withdraw_stall_dish", dict(menu_id=menu.pk), player=self.b)
        self.assertEqual(foreign["result_code"], "MENU_NOT_OWNED")
        withdrawn = self.act("withdraw_stall_dish", dict(menu_id=menu.pk))
        self.assertTrue(withdrawn["success"], withdrawn)
        returned = self.bag_a.slots.get(instance_id=withdrawn["data"]["result"]["returned_instance_id"])
        self.assertEqual({f: getattr(returned, f) for f in BATCH_COPY_FIELDS}, original)
        self.assertEqual(sum(self.bag_a.slots.filter(item_id=29201).values_list("item_amount", flat=True)), 3)
        self.assertFalse(SkillRewardReceipt.objects.filter(player=self.a, skill_code="business").exists())

    def test_capacity_freshness_public_identity_and_expiry_reject_without_transfer(self):
        self.opened()
        source = self.food(6)
        base = dict(instance_id=str(source.instance_id), quantity=6)
        self.assertEqual(self.act("list_stall_dish", base)["result_code"], "MENU_CAPACITY_REACHED")
        base["quantity"] = 1
        for fields, code in ((dict(expires_world_minute=2000, dish_metadata={"public_meal_claim": {"world_minute": 100}}), "PUBLIC_MEAL_NOT_RESALE"),):
            for name, value in fields.items():
                setattr(source, name, value)
            source.save(update_fields=list(fields))
            self.assertEqual(self.act("list_stall_dish", base)["result_code"], code)
        source.refresh_from_db()
        self.assertEqual(source.item_amount, 6)
        self.assertFalse(MenuItem.objects.filter(player=self.a).exists())

    def test_last_portion_tick_receipt_ledger_and_experience_settle_once_without_http(self):
        self.opened(); menu = self.listed(quantity=1); hourly = self.schedule()
        before = self.bag_a.money
        with patch("requests.post", side_effect=AssertionError("Tick must not call HTTP")):
            self.at(605)
            self.assertEqual(process_stall_tick(self.clock), [str(self.a.pk)])
            self.assertEqual(process_stall_tick(self.clock), [])
            self.at(607)
            self.assertEqual(process_stall_tick(self.clock), [])
        self.bag_a.refresh_from_db(); hourly.refresh_from_db()
        self.assertEqual((self.bag_a.money - before, hourly.sold_count), (12, 1))
        self.assertFalse(MenuItem.objects.filter(pk=menu.pk).exists())
        self.assertEqual(CustomerTransaction.objects.filter(player=self.a).count(), 1)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="business").total_xp, 25)
        self.assertEqual(EconomicLedgerEntry.objects.get(entry_type="PLAYER_STALL_SALE").external_delta, 12)
        self.assertEqual(hourly.sale_events.filter(processed=True, skipped=True).count(), 2)
        self.assertFalse(PlayerSkillProgress.objects.filter(player=self.b).exists())

    def test_schedules_before_listing_expired_at_tick_and_time_jumps_never_back_sell(self):
        self.opened(); self.schedule((1, 2, 3))
        self.at(605)
        source = self.food(2, expires_world_minute=620)
        self.listed(source, 2)
        self.at(610); self.assertEqual(process_stall_tick(self.clock), [])
        self.at(12 * 60 + 59); self.assertEqual(process_stall_tick(self.clock), [])
        self.at(3 * 1440 + 10 * 60); self.assertEqual(process_stall_tick(self.clock), [])
        self.assertEqual(CustomerTransaction.objects.filter(player=self.a).count(), 0)
        self.assertEqual(RestaurantHourlySlot.objects.filter(player=self.a).count(), 3)
        self.assertFalse(SkillRewardReceipt.objects.filter(player=self.a, skill_code="business").exists())

    def test_real_market_purchase_cost_is_known_and_self_production_margin_is_unknown(self):
        source = self.food(2, bag=self.bag_b)
        listing, error = create_listing(self.b, source.instance_id, 2, 7)
        self.assertIsNone(error)
        purchased = self.run_action("buy_marketplace", dict(listing_id=str(listing.pk), quantity=2, expected_unit_price=7), location="MARKET_STREET")
        self.assertTrue(purchased["success"], purchased)
        received = self.bag_a.slots.get(instance_id=purchased["data"]["result"]["purchased_batches"][0]["instance_id"])
        self.opened(); menu = self.listed(received, 1)
        self.assertEqual(menu.purchase_unit_cost, 7)
        self.schedule((5,)); self.at(605); process_stall_tick(self.clock)
        summary = business_overview(self.a, self.bag_a, 605)["summary"]
        self.assertEqual(summary, dict(revenue=12, known_cost=7, net_income_nullable=5, cost_complete=True, sold_count=1))
        self.listed(self.food(1), 1)
        hourly = RestaurantHourlySlot.objects.get(player=self.a)
        hourly.quota = 2; hourly.save(update_fields=["quota"])
        RestaurantSaleEvent.objects.create(slot=hourly, scheduled_minute=6)
        self.at(606); process_stall_tick(self.clock)
        summary = business_overview(self.a, self.bag_a, 606)["summary"]
        self.assertEqual((summary["known_cost"], summary["net_income_nullable"], summary["cost_complete"]), (7, None, False))

    def test_legacy_unknown_menu_returns_unknown_and_legacy_batch_safe_return_is_lossless(self):
        legacy = MenuItem.objects.create(player=self.a, instance_id=str(uuid.uuid4()), item_id=29201,
            item_name="历史菜品", quantity=2, dish_quality=4, rated=True, rating_price=12)
        row = business_overview(self.a, self.bag_a, 600)["menu"][0]
        self.assertEqual((row["reason"], row["purchase_unit_cost"]), ("LEGACY_BATCH_UNKNOWN", None))
        returned = self.act("withdraw_stall_dish", dict(menu_id=legacy.pk))
        self.assertTrue(returned["data"]["result"]["freshness_unknown"])
        self.opened(); source = self.food(); menu = self.listed(source, 1)
        before = {field: copy.deepcopy(getattr(menu.batch_slot, field)) for field in BATCH_COPY_FIELDS}
        ok, _ = RestaurantService.withdraw_dish(self.a, menu.pk)
        self.assertTrue(ok)
        new = self.bag_a.slots.order_by("-slot_index").first()
        self.assertEqual({field: getattr(new, field) for field in BATCH_COPY_FIELDS}, before)
        missing = self.listed(source, 1)
        missing.batch_slot.delete()
        refused = self.act("withdraw_stall_dish", dict(menu_id=missing.pk))
        self.assertEqual(refused["result_code"], "BATCH_MISSING")

    def test_upgrade_exact_cost_rewards_knowledge_replay_and_missing_materials_are_atomic(self):
        state = self.opened()
        PlayerSkillProgress.objects.update_or_create(player=self.a, skill_code="business", defaults=dict(total_xp=80))
        state.total_dishes_sold = 5; state.save(update_fields=["total_dishes_sold"])
        recipe = Recipe.objects.create(item_id=29201, recipe_name="设施奖励菜谱")
        Item.objects.update_or_create(pk=3006, defaults=dict(item_name="奖励材料", item_type=ItemType.COMMODITY))
        RestaurantLevelConfig.objects.update_or_create(level=2, defaults=dict(required_dishes_sold=5,
            cost_money=50, cost_items=[{"itemId": 1002, "amount": 4}],
            reward_materials=[{"itemId": 3006, "amount": 2}], reward_recipes=[recipe.recipe_name]))
        wood = self.slot(3, item_id=1002, expires_world_minute=None)
        quote = business_overview(self.a, self.bag_a, 600)["upgrade"]
        self.assertEqual(quote["reason"], "INSUFFICIENT_MATERIALS")
        failed = self.act("upgrade_player_stall", dict(expected_quote=quote["quote_hash"]))
        self.assertEqual(failed["result_code"], "INSUFFICIENT_MATERIALS")
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 100)
        self.assertFalse(PlayerRecipe.objects.filter(recipe_name=recipe.recipe_name).exists())
        wood.item_amount = 4; wood.save(update_fields=["item_amount"])
        quote = business_overview(self.a, self.bag_a, 600)["upgrade"]
        payload = self.payload("upgrade_player_stall", dict(expected_quote=quote["quote_hash"]), location="PLAYER_STALL")
        with patch.object(VillagePlayerActionReceipt.objects, "create", side_effect=RuntimeError("receipt unavailable")):
            with self.assertRaises(RuntimeError):
                execute_player_action(payload)
        wood.refresh_from_db(); self.bag_a.refresh_from_db(); state.refresh_from_db()
        self.assertEqual((wood.item_amount, self.bag_a.money, state.current_level), (4, 100, 1))
        upgraded = execute_player_action(payload)
        self.assertTrue(upgraded["success"], upgraded)
        self.assertEqual(dict(upgraded, replayed=True), execute_player_action(copy.deepcopy(payload)))
        wood.refresh_from_db(); self.bag_a.refresh_from_db(); state.refresh_from_db()
        self.assertEqual((wood.item_amount, self.bag_a.money, state.current_level), (0, 50, 2))
        self.assertEqual(self.bag_a.slots.get(item_id=3006).item_amount, 2)
        knowledge = PlayerRecipe.objects.get(recipe_name=recipe.recipe_name)
        self.assertEqual((knowledge.catalog_recipe_id, knowledge.show_ingredient_count), (recipe.pk, False))

    def test_sale_failure_rolls_back_inventory_money_event_and_experience(self):
        self.opened(); menu = self.listed(quantity=1); hourly = self.schedule((5,))
        self.at(605)
        with patch.object(EconomicLedgerEntry.objects, "create", side_effect=RuntimeError("ledger unavailable")):
            with self.assertRaises(RuntimeError):
                process_stall_tick(self.clock)
        menu.refresh_from_db(); self.bag_a.refresh_from_db(); hourly.refresh_from_db()
        self.assertEqual((menu.quantity, menu.batch_slot.item_amount, self.bag_a.money, hourly.sold_count), (1, 1, 100, 0))
        self.assertFalse(CustomerTransaction.objects.filter(player=self.a).exists())
        self.assertFalse(PlayerMilestoneProgress.objects.filter(player=self.a, code="first_stall_sale").exists())
        self.assertFalse(hourly.sale_events.get().processed)

    def test_default_first_facility_migration_preserves_custom_configuration(self):
        config, _ = RestaurantLevelConfig.objects.update_or_create(level=2, defaults=dict(
            required_total_ratings=20, required_dishes_sold=50, cost_money=500, cost_items=[],
            required_dish_ids=[], required_dish_scores=[], reward_materials=[], reward_recipes=[]))
        migration = importlib.import_module("restaurant.migrations.0013_player_stall_batches")
        migration.improve_default_first_level(apps, SimpleNamespace(connection=connection))
        config.refresh_from_db()
        self.assertEqual((config.required_total_ratings, config.required_dishes_sold, config.cost_money, config.cost_items),
                         (0, 5, 50, [{"itemId": 1002, "amount": 4}]))
        config.cost_money = 777; config.save(update_fields=["cost_money"])
        migration.improve_default_first_level(apps, SimpleNamespace(connection=connection))
        config.refresh_from_db(); self.assertEqual(config.cost_money, 777)
