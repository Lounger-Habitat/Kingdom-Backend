"""Focused checks for precise, server-settled player cooking."""
import copy
import importlib
from types import SimpleNamespace
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from account.models import GameAccount
from cooking.models import PityConfig, PityCounter
from inventory.components import copy_batch_attributes
from inventory.models import InventoryBag, InventorySlot, NpcDefinition
from items.models import Item, ItemType
from player.models import (PlayerMilestoneProgress, PlayerRecipe, PlayerSkillProgress,
                           PlayerState, SkillRewardReceipt)
from recipes.models import Recipe, RecipeIngredient
from village.lucy_catalog import install_catalog
from village.models import (EconomicLedgerEntry, VillagePlayerActionReceipt, VillageStore,
                            VillageStoreAccount, VillageStoreListing)
from village.player_actions import execute_player_action
from village.player_cooking import cooking_overview, is_player_cooked, preview_cooking
from village.production_tools import ensure_production_catalog, execute_tool_action, tools_overview
from village.tests.test_player_actions import PlayerFixture


class PlayerCookingTests(PlayerFixture, TestCase):
    def _manual_start(self, raw, cut="WHOLE"):
        params = dict(self.params(raw), cut_style=cut)
        preview = preview_cooking(self.a, self.bag_a, params, self.clock.world_minute)
        self.assertTrue(preview["available"], preview)
        payload = self.payload("start_cooking", dict(params, expected_quote=preview["quote_hash"]), location="RESTAURANT")
        answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer)
        return answer["data"]["result"]["cooking_session"], payload

    def test_manual_session_settles_once_and_proves_cooking_without_control_rewards(self):
        raw = self.raw()
        now = timezone.now()
        forbidden = AssertionError("Process-only controls must not run world or reward work")
        with patch("village.player_cooking_sessions.timezone.now", return_value=now), \
             patch("village.daily_events.evaluate_daily_events", side_effect=forbidden), \
             patch("player.starter.post_action_progress", side_effect=forbidden), \
             patch("village.personal_commissions.expire_commissions", side_effect=forbidden):
            session, _ = self._manual_start(raw)
        self.assert_uncharged(raw)
        with patch("village.player_cooking_sessions.timezone.now", return_value=now + timedelta(seconds=3)):
            tuned = self.run_action("set_cooking_heat", dict(session_id=session["session_id"], heat="MEDIUM"), location="RESTAURANT")
        self.assertTrue(tuned["success"], tuned)
        self.assertEqual(tuned["data"]["result"]["cooking_session"]["progress"], 30)
        self.assert_uncharged(raw)
        payload = self.payload("cook_dish", dict(session_id=session["session_id"]), location="RESTAURANT")
        with patch("village.player_cooking_sessions.timezone.now", return_value=now + timedelta(seconds=9)):
            cooked = execute_player_action(payload)
            replay = execute_player_action(copy.deepcopy(payload))
            duplicate = self.run_action("cook_dish", payload["params"], location="RESTAURANT")
        self.assertTrue(cooked["success"], cooked)
        self.assertTrue(replay["replayed"])
        self.assertEqual(duplicate["result_code"], "COOKING_SESSION_FINISHED")
        dish = cooked["data"]["result"]["dish"]
        self.assertEqual(dish["quality"], 4)
        self.assertEqual(dish["dish_metadata"]["cook_time_source"], "MANUAL_COOKING")
        self.assertTrue(is_player_cooked(self.bag_a.slots.get(instance_id=dish["instance_id"]), self.a))
        raw.refresh_from_db(); self.tool.refresh_from_db(); self.state.refresh_from_db()
        self.assertEqual((raw.item_amount, self.tool.remaining_uses, self.state.action_points), (4, 19, 195))
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="PLAYER_COOK").count(), 1)
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a, source_type="player_cooking").count(), 1)

    def test_manual_bad_cut_fire_and_late_finish_lower_quality(self):
        raw = self.raw()
        now = timezone.now()
        with patch("village.player_cooking_sessions.timezone.now", return_value=now):
            session, _ = self._manual_start(raw, cut="SLICE")
        with patch("village.player_cooking_sessions.timezone.now", return_value=now + timedelta(seconds=16)):
            cooked = self.run_action("cook_dish", dict(session_id=session["session_id"]), location="RESTAURANT")
        self.assertTrue(cooked["success"], cooked)
        dish = cooked["data"]["result"]["dish"]
        self.assertLess(dish["quality"], 4)
        self.assertEqual(cooked["data"]["result"]["cooking_session"]["progress"], 100)
        feedback = dish["dish_metadata"]["cooking_feedback"]
        self.assertIn("不同", feedback["cut"])
        self.assertIn("不匹配", feedback["heat"])
        self.assertIn("偏晚", feedback["timing"])

    def test_manual_cancel_and_old_start_replay_never_revive_a_session(self):
        raw = self.raw()
        session, payload = self._manual_start(raw)
        cancelled = self.run_action("cancel_cooking", dict(session_id=session["session_id"]), location="")
        self.assertTrue(cancelled["success"], cancelled)
        replay = execute_player_action(payload)
        self.assertEqual(replay["data"]["result"]["cooking_session"]["status"], "CANCELLED")
        self.assertIsNone(cooking_overview(self.a, self.bag_a, 100)["active_session"])
        newer, _ = self._manual_start(raw)
        self.assertNotEqual(newer["session_id"], session["session_id"])
        self.assertIsNone(execute_player_action(payload)["data"]["result"]["cooking_session"])
        self.assert_uncharged(raw)

    def test_manual_finish_revalidates_inputs_and_rolls_back_costs(self):
        raw = self.raw()
        session, _ = self._manual_start(raw)
        raw.item_amount = 5; raw.save(update_fields=["item_amount"])
        failed = self.run_action("cook_dish", dict(session_id=session["session_id"]), location="RESTAURANT")
        self.assertEqual(failed["result_code"], "QUOTE_CHANGED")
        self.assert_uncharged(raw, 5)
        self.assertEqual(cooking_overview(self.a, self.bag_a, 100)["active_session"]["session_id"], session["session_id"])

    def test_manual_rejects_forged_progress_foreign_sessions_and_expiry(self):
        raw = self.raw()
        now = timezone.now()
        with patch("village.player_cooking_sessions.timezone.now", return_value=now):
            session, _ = self._manual_start(raw)
        for action, params, player in (
            ("set_cooking_heat", dict(session_id=session["session_id"], heat="LOW", progress=80), self.a),
            ("set_cooking_heat", dict(session_id=session["session_id"], heat=[]), self.a),
            ("set_cooking_heat", dict(session_id=session["session_id"], heat="LOW"), self.b),
            ("cook_dish", dict(session_id=session["session_id"]), self.b),
        ):
            reply = self.run_action(action, params, player=player, location="RESTAURANT")
            self.assertFalse(reply["success"], reply)
        with patch("village.player_cooking_sessions.timezone.now", return_value=now + timedelta(seconds=121)):
            expired = self.run_action("cook_dish", dict(session_id=session["session_id"]), location="RESTAURANT")
            self.assertEqual(expired["result_code"], "COOKING_SESSION_EXPIRED")
            self.assertIsNone(cooking_overview(self.a, self.bag_a, 100)["active_session"])
        self.assert_uncharged(raw)

    def test_quality_bonus_is_rolled_per_standard_batch_and_each_output_has_proof(self):
        from unittest.mock import Mock
        from player.models import EquipmentLevel
        self.level()
        self.tool.equipment_level = 3
        self.tool.save(update_fields=["equipment_level"])
        EquipmentLevel.objects.filter(equipment__item_id=4106, level=3).update(quality_bonus_chance=.5)
        raw = self.raw(8)
        rng = Mock()
        rng.randint.return_value = 0
        rng.random.side_effect = [0, .1, 0, .9, 0, .1]
        with patch("village.player_cooking.quality_rng", return_value=rng), patch("village.player_cooking.calculate_quality", return_value=3):
            answer, _ = self.cook(self.params(raw, quantity=6, batches=3))
        self.assertTrue(answer["success"], answer)
        outputs = answer["data"]["result"]["items_added"]
        self.assertEqual({row["quality"]: row["quantity"] for row in outputs}, {4: 4, 3: 2})
        self.assertTrue(all(is_player_cooked(self.bag_a.slots.get(instance_id=row["instance_id"]), self.a) for row in outputs))

    def setUp(self):
        super().setUp()
        for item_id in (1049, 1056, 1009, 1034, 1057, 1077, 1025, 1042, 1023):
            Item.objects.update_or_create(item_id=item_id, defaults=dict(item_name=str(item_id),
                item_type=ItemType.SEASONING if item_id == 1049 else ItemType.INGREDIENT))
        Item.objects.get_or_create(item_id=1078, defaults=dict(item_name="失败的菜品", item_type=ItemType.FOOD))
        install_catalog()
        self.state = PlayerState.objects.create(player_id=str(self.a.pk), action_points=200)
        self.tool = InventorySlot.objects.create(bag=self.bag_a, slot_index=0, item_id=4106,
                                                 item_amount=1, remaining_uses=20)
        Item.objects.create(item_id=61001, item_name="普通探索汤", item_type=ItemType.FOOD)
        self.ordinary = Recipe.objects.create(pk=81001, item_id=61001, recipe_name="普通探索汤",
            default_cook_time=60, output_quantity=2)
        for item_id, count in ((1030, 2), (1049, 1)):
            RecipeIngredient.objects.create(recipe=self.ordinary, item_id=item_id, item_name=str(item_id), count=count)

    def raw(self, amount=6, item_id=1030, **kwargs):
        return self.slot(amount, item_id=item_id, source_request_id=uuid.uuid4(), **kwargs)

    def params(self, raw, quantity=2, recipe_id=29201, batches=1, tool=None):
        return dict(recipe_id=recipe_id, batches=batches, tool_instance_id=str((tool or self.tool).instance_id),
                    selections=[dict(instance_id=str(raw.instance_id), quantity=quantity)])

    def cook(self, params, rid=None):
        preview = preview_cooking(self.a, self.bag_a, params, self.clock.world_minute)
        self.assertTrue(preview["available"], preview)
        payload = self.payload("cook_dish", dict(params, expected_quote=preview["quote_hash"]),
                               rid=rid, location="RESTAURANT")
        return execute_player_action(payload), payload

    def level(self, xp=440):
        PlayerSkillProgress.objects.update_or_create(player=self.a, skill_code="cooking", defaults=dict(total_xp=xp))

    def assert_uncharged(self, raw, amount=6):
        raw.refresh_from_db(); self.tool.refresh_from_db(); self.state.refresh_from_db()
        self.assertEqual((raw.item_amount, self.tool.remaining_uses, self.state.action_points), (amount, 20, 200))
        self.assertFalse(SkillRewardReceipt.objects.filter(player=self.a, source_type="player_cooking").exists())

    def test_ingredient_overview_exposes_real_categories_for_backpack_filter(self):
        Item.objects.filter(pk=1030).update(item_type=ItemType.VEGETABLE)
        self.raw()
        self.raw(item_id=1049, expires_world_minute=None)
        ingredients = {row["item_id"]: row for row in cooking_overview(self.a, self.bag_a, 100)["ingredients"]}
        self.assertEqual(ingredients[1030]["item_type"], ItemType.VEGETABLE)
        self.assertEqual(ingredients[1049]["item_type"], ItemType.SEASONING)

    def test_one_cook_after_preview_advances_both_preparation_and_cooking_lessons(self):
        from player.models import PlayerTutorialState
        from player.tutorial_catalog import VERSION
        PlayerTutorialState.objects.create(player=self.a, version=VERSION, started=True, route_code="cooking",
            completed={"cooking_recipe": "fixture"})
        raw = self.raw(expires_world_minute=1000)
        preview = self.run_action("preview_cooking", self.params(raw), location="RESTAURANT")
        self.assertTrue(preview["success"], preview)
        answer, payload = self.cook(self.params(raw))
        self.assertTrue(answer["success"], answer)
        tutorial = answer["data"]["overview"]["tutorial"]
        self.assertEqual(tutorial["current_task"]["code"], "cooking_review")
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("cooking_prepare", state.completed)
        self.assertIn("cooking_first", state.completed)
        self.assertNotIn("cooking_review", state.completed)
        self.assertEqual(execute_player_action(copy.deepcopy(payload)), dict(answer, replayed=True))
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a, source_type="player_cooking").count(), 1)

    def test_exact_batch_real_receipt_and_retry_do_not_duplicate_assets_or_xp(self):
        raw = self.raw(expires_world_minute=1000)
        cooked, payload = self.cook(self.params(raw))
        self.assertTrue(cooked["success"], cooked)
        dish = cooked["data"]["result"]["dish"]
        self.assertEqual(cooked["data"]["settlement_section"], "cooking")
        self.assertTrue(cooked["data"]["overview_partial"])
        self.assertNotIn("inventory_partial", cooked["data"])
        self.assertIn(dish["instance_id"], [row["instance_id"] for row in cooked["data"]["overview"]["inventory"]])
        self.assertEqual((dish["quantity"], dish["produced_world_minute"], dish["expires_world_minute"]), (2, 100, None))
        self.assertEqual(cooked["data"]["result"]["growth_rewards"][0]["xp_gained"], 40)
        self.assertEqual(dict(cooked, replayed=True), execute_player_action(copy.deepcopy(payload)))
        raw.refresh_from_db(); self.tool.refresh_from_db(); self.state.refresh_from_db()
        self.assertEqual((raw.item_amount, self.tool.remaining_uses, self.state.action_points), (4, 19, 195))
        self.assertEqual(self.bag_a.slots.filter(item_id=29201).count(), 1)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="cooking").total_xp, 40)
        metadata = dish["dish_metadata"]
        self.assertEqual(metadata["maker_player_id"], str(self.a.pk))
        self.assertEqual(metadata["production_request_id"], payload["request_id"])
        self.assertNotIn("maker_npc_id", metadata)
        self.assertTrue(is_player_cooked(self.bag_a.slots.get(instance_id=dish["instance_id"]), self.a))

    def test_three_standard_batches_require_t2_and_charge_per_batch(self):
        self.level()
        raw = self.raw(8)
        params = self.params(raw, quantity=6, batches=3)
        self.assertEqual(preview_cooking(self.a, self.bag_a, params, 100)["reason"], "TOOL_LEVEL_REQUIRED")
        better = InventorySlot.objects.create(bag=self.bag_a, slot_index=2, item_id=4107,
                                              item_amount=1, remaining_uses=50, equipment_level=3)
        params["tool_instance_id"] = str(better.instance_id)
        cooked, _ = self.cook(params)
        self.assertTrue(cooked["success"], cooked)
        result = cooked["data"]["result"]
        self.assertEqual((sum(row["quantity"] for row in result["items_added"]), result["action_cost"], result["tool_used"]["uses"]), (6, 15, 3))
        better.refresh_from_db(); self.state.refresh_from_db(); raw.refresh_from_db()
        self.assertEqual((better.remaining_uses, self.state.action_points, raw.item_amount), (47, 185, 2))
        self.assertEqual(result["growth_rewards"][0]["xp_gained"], 80)
        self.assertEqual(result["growth_rewards"][0]["facts"]["standard_batches"], 3)

    def test_level_two_keeps_low_quality_possible_and_real_meat_variant_level_gate(self):
        raw = self.raw()
        with patch("village.player_cooking.calculate_quality", return_value=2):
            self.level(100)
            cooked, _ = self.cook(self.params(raw))
        self.assertIn(cooked["data"]["result"]["dish"]["quality"], (2, 3))
        meat = self.raw(item_id=1009)
        salt = self.raw(item_id=1049, expires_world_minute=None)
        params = self.params(raw, quantity=1, recipe_id=29212)
        params["selections"] += [dict(instance_id=str(meat.instance_id), quantity=1),
                                  dict(instance_id=str(salt.instance_id), quantity=1)]
        self.assertEqual(preview_cooking(self.a, self.bag_a, params, 100)["reason"], "SKILL_LEVEL_REQUIRED")

    def test_legacy_freshness_is_ignored_but_duplicate_and_foreign_batches_fail_without_charge(self):
        raw = self.raw(expires_world_minute=100)
        params = self.params(raw)
        self.assertTrue(preview_cooking(self.a, self.bag_a, params, 100)["available"])
        raw.expires_world_minute = 1000; raw.freshness_unknown = True
        raw.save(update_fields=["expires_world_minute", "freshness_unknown"])
        self.assertTrue(preview_cooking(self.a, self.bag_a, params, 100)["available"])
        raw.freshness_unknown = False; raw.save(update_fields=["freshness_unknown"])
        params["selections"] *= 2
        self.assertEqual(preview_cooking(self.a, self.bag_a, params, 100)["reason"], "DUPLICATE_INGREDIENT_BATCH")
        foreign = self.slot(6, bag=self.bag_b, source_request_id=uuid.uuid4())
        params = self.params(foreign)
        failed = self.run_action("cook_dish", dict(params, expected_quote="forged"), location="RESTAURANT")
        self.assertEqual(failed["result_code"], "INGREDIENT_NOT_OWNED")
        self.assert_uncharged(raw)

    def test_legacy_perishables_cook_normally_with_provenance_and_experience(self):
        raw = self.raw(expires_world_minute=None)
        self.assertTrue(preview_cooking(self.a, self.bag_a, self.params(raw), 100)["available"])
        salt = self.raw(item_id=1049, expires_world_minute=None)
        params = self.params(raw, recipe_id=None)
        params["selections"].append(dict(instance_id=str(salt.instance_id), quantity=1))
        cooked, _ = self.cook(params)
        self.assertTrue(cooked["success"], cooked)
        dish = cooked["data"]["result"]["dish"]
        self.assertFalse(dish["freshness_unknown"])
        self.assertIsNone(dish["expires_world_minute"])
        self.assertEqual(dish["item_id"], 61001)
        self.assertTrue(is_player_cooked(self.bag_a.slots.get(instance_id=dish["instance_id"]), self.a))
        self.assertTrue(PlayerMilestoneProgress.objects.filter(player=self.a, code="first_player_cooking").exists())

    def test_hidden_knowledge_and_discovery_keep_preview_private_then_unlock_exact_cook(self):
        learned = PlayerRecipe.objects.create(player=self.state, recipe_name=self.ordinary.recipe_name,
                                               catalog_recipe=self.ordinary, show_ingredient_count=False)
        raw = self.raw()
        params = self.params(raw, recipe_id=self.ordinary.pk)
        rows = cooking_overview(self.a, self.bag_a, 100)["recipes"]
        row = next(r for r in rows if r["id"] == self.ordinary.pk)
        self.assertEqual([i["count"] for i in row["ingredients"]], [None, None])
        preview = preview_cooking(self.a, self.bag_a, params, 100)
        self.assertTrue(preview["available"], preview)
        self.assertIsNone(preview["output_quantity"])
        self.assertEqual(preview["missing_items"], [])
        learned.delete()
        salt = self.raw(item_id=1049, expires_world_minute=None)
        params = self.params(raw, recipe_id=None)
        params["selections"].append(dict(instance_id=str(salt.instance_id), quantity=1))
        preview = preview_cooking(self.a, self.bag_a, params, 100)
        self.assertEqual((preview["recipe_id"], preview["output_item_id"], preview["output_quantity"]), (None, None, None))
        cooked, _ = self.cook(params)
        self.assertTrue(cooked["success"], cooked)
        self.assertTrue(PlayerRecipe.objects.get(player=self.state, recipe_name=self.ordinary.recipe_name).show_ingredient_count)
        self.assertTrue(cooked["data"]["result"]["recipe_quantities_unlocked"])

    def test_correct_ratio_upgrades_existing_hidden_recipe_and_retry_is_idempotent(self):
        learned = PlayerRecipe.objects.create(player=self.state, recipe_name=self.ordinary.recipe_name,
                                               show_ingredient_count=False)
        raw = self.raw(expires_world_minute=1000)
        salt = self.raw(item_id=1049, expires_world_minute=None)
        params = self.params(raw, recipe_id=self.ordinary.pk)
        params["selections"].append(dict(instance_id=str(salt.instance_id), quantity=1))
        cooked, payload = self.cook(params)
        self.assertTrue(cooked["success"], cooked)
        self.assertTrue(cooked["data"]["result"]["recipe_quantities_unlocked"])
        learned.refresh_from_db()
        self.assertEqual((learned.show_ingredient_count, learned.catalog_recipe_id), (True, self.ordinary.pk))
        row = next(r for r in cooked["data"]["overview"]["cooking"]["recipes"] if r["id"] == self.ordinary.pk)
        self.assertEqual([i["count"] for i in row["ingredients"]], [2, 1])
        self.assertEqual(dict(cooked, replayed=True), execute_player_action(copy.deepcopy(payload)))
        self.assertEqual(PlayerRecipe.objects.filter(player=self.state, recipe_name=self.ordinary.recipe_name).count(), 1)

    def test_wrong_ordinary_ratios_and_failed_exploration_give_no_success_xp(self):
        raw = self.raw()
        salt = self.raw(item_id=1049, expires_world_minute=None)
        params = self.params(raw, quantity=1, recipe_id=None)
        params["selections"].append(dict(instance_id=str(salt.instance_id), quantity=1))
        cooked, _ = self.cook(params)
        self.assertTrue(cooked["success"], cooked)
        dish = cooked["data"]["result"]["dish"]
        self.assertEqual((dish["item_id"], dish["quantity"]), (61001, 1))
        self.assertFalse(cooked["data"]["result"]["recipe_quantities_unlocked"])
        self.assertFalse(PlayerRecipe.objects.get(player=self.state, recipe_name=self.ordinary.recipe_name).show_ingredient_count)
        self.assertFalse(is_player_cooked(self.bag_a.slots.get(instance_id=dish["instance_id"]), self.a))
        failed, _ = self.cook(self.params(salt, quantity=1, recipe_id=None))
        self.assertEqual((failed["data"]["result"]["dish"]["item_id"], failed["data"]["result"]["dish"]["quality"]), (1078, 1))
        self.assertFalse(SkillRewardReceipt.objects.filter(player=self.a, source_type="player_cooking").exists())

    def test_stale_quote_and_receipt_failure_roll_back_cost_output_knowledge_and_xp(self):
        raw = self.raw()
        params = self.params(raw)
        preview = preview_cooking(self.a, self.bag_a, params, 100)
        raw.item_amount = 7; raw.save(update_fields=["item_amount"])
        failed = self.run_action("cook_dish", dict(params, expected_quote=preview["quote_hash"]), location="RESTAURANT")
        self.assertEqual(failed["result_code"], "QUOTE_CHANGED")
        self.assert_uncharged(raw, 7)
        preview = preview_cooking(self.a, self.bag_a, params, 100)
        with patch.object(VillagePlayerActionReceipt.objects, "create", side_effect=RuntimeError("receipt unavailable")):
            with self.assertRaises(RuntimeError):
                self.run_action("cook_dish", dict(params, expected_quote=preview["quote_hash"]), location="RESTAURANT")
        self.assert_uncharged(raw, 7)
        self.assertFalse(self.bag_a.slots.filter(item_id=29201).exists())
        self.assertFalse(PlayerRecipe.objects.filter(player=self.state).exists())

    def test_readonly_overview_preview_unsaved_bag_and_visible_shortage_make_no_writes(self):
        raw = self.raw(1)
        params = self.params(raw)
        newcomer = GameAccount.objects.create(username="cooking-newcomer")
        unsaved = InventoryBag(player=newcomer, character_id=newcomer.username)
        with CaptureQueriesContext(connection) as queries:
            overview = cooking_overview(self.a, self.bag_a, 100, read_only=True)
            preview = self.run_action("preview_cooking", params)["data"]["result"]["preview"]
            empty = preview_cooking(newcomer, unsaved, params, 100)
            cooking_overview(newcomer, unsaved, 100, read_only=True)
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertEqual(overview["observed_world_minute"], 100)
        self.assertEqual(empty["reason"], "INGREDIENT_NOT_OWNED")
        self.assertEqual(preview["missing_items"], [dict(item_id=1030, item_name="玉米", quantity=1,
            location_code="BILL_FARM", reason="INGREDIENT_REQUIRED")])

    def test_production_proof_survives_sample_split_but_rejects_buyer_or_tampered_batch(self):
        raw = self.raw()
        cooked, _ = self.cook(self.params(raw))
        source = self.bag_a.slots.get(instance_id=cooked["data"]["result"]["dish"]["instance_id"])
        sample = InventorySlot(bag=self.bag_a, slot_index=8, item_id=source.item_id, item_amount=1)
        copy_batch_attributes(source, sample); sample.save()
        self.assertNotEqual(sample.instance_id, source.instance_id)
        self.assertTrue(is_player_cooked(sample, self.a))
        self.assertFalse(is_player_cooked(sample, self.b))
        sample.quality += 1
        self.assertFalse(is_player_cooked(sample, self.a))

    def test_original_single_batch_pity_is_inherited_and_rollback_or_retry_never_consumes_twice(self):
        PityConfig.get_config()
        counter = PityCounter.objects.create(player=self.a, count_4=7, count_5=12, count_6=89)
        raw, salt = self.raw(), self.raw(item_id=1049, expires_world_minute=None)
        params = self.params(raw, recipe_id=None)
        params["selections"].append(dict(instance_id=str(salt.instance_id), quantity=1))
        preview = preview_cooking(self.a, self.bag_a, params, 100)
        self.assertTrue(any("旧保底" in warning for warning in preview["warnings"]))
        counter.refresh_from_db()
        self.assertEqual((counter.count_4, counter.count_5, counter.count_6), (7, 12, 89))
        with patch("village.player_cooking.calculate_quality", return_value=2):
            with patch.object(VillagePlayerActionReceipt.objects, "create", side_effect=RuntimeError("receipt unavailable")):
                with self.assertRaises(RuntimeError):
                    self.run_action("cook_dish", dict(params, expected_quote=preview["quote_hash"]), location="RESTAURANT")
            counter.refresh_from_db()
            self.assertEqual((counter.count_4, counter.count_5, counter.count_6), (7, 12, 89))
            self.assert_uncharged(raw)
            cooked, payload = self.cook(params)
        self.assertTrue(cooked["success"], cooked)
        dish = cooked["data"]["result"]["dish"]
        self.assertEqual((dish["quality"], dish["cook_time_seconds"]), (6, 60))
        self.assertEqual(dish["dish_metadata"]["cook_time_source"], "LEGACY_PITY_SECONDS")
        self.assertEqual(dict(cooked, replayed=True), execute_player_action(copy.deepcopy(payload)))
        counter.refresh_from_db()
        self.assertEqual((counter.count_4, counter.count_5, counter.count_6), (0, 0, 0))
        # Neither ordinary multi-batch nor village cooking can spend old pity.
        counter.count_6 = 89; counter.save(update_fields=["count_6"])
        self.level()
        self.tool.item_id = 4107; self.tool.remaining_uses = 50; self.tool.equipment_level = 3
        self.tool.save(update_fields=["item_id", "remaining_uses", "equipment_level"])
        raw.item_amount = 8; raw.save(update_fields=["item_amount"])
        PlayerRecipe.objects.filter(player=self.state, catalog_recipe=self.ordinary).update(show_ingredient_count=True)
        multiple = self.params(raw, quantity=6, recipe_id=self.ordinary.pk, batches=3)
        multiple["selections"].append(dict(instance_id=str(salt.instance_id), quantity=3))
        cooked, _ = self.cook(multiple)
        self.assertTrue(cooked["success"], cooked)
        village, _ = self.cook(self.params(raw, quantity=2))
        self.assertTrue(village["success"], village)
        counter.refresh_from_db()
        self.assertEqual((counter.count_4, counter.count_5, counter.count_6), (0, 0, 89))

    def test_rollout_and_runtime_share_finite_cookware_stock_and_upgrade_keeps_instance(self):
        nora, _ = NpcDefinition.objects.get_or_create(agent_id="10015", defaults=dict(name="Nora"))
        nora.is_test_only = False; nora.save(update_fields=["is_test_only"])
        store, _ = VillageStore.objects.update_or_create(store_code="GENERAL_STORE", defaults=dict(
            display_name="杂货店", location_code="GENERAL_STORE", operator_npc=nora, status="OPEN"))
        account, _ = VillageStoreAccount.objects.get_or_create(store=store, defaults=dict(balance=10))
        balance_before = account.balance
        migration = importlib.import_module("items.migrations.0010_player_cooking_tools")
        ensure_production_catalog(100)
        listing = VillageStoreListing.objects.get(store=store, item_id=4106)
        self.assertEqual(listing.inventory_slots.count(), 20)
        self.assertEqual({(s.item_amount, s.remaining_uses) for s in listing.inventory_slots.all()}, {(1, 20)})
        ensure_production_catalog(100)
        ensure_production_catalog(100)
        listing.delete()
        ensure_production_catalog(200)
        ensure_production_catalog(100)
        self.assertFalse(VillageStoreListing.objects.filter(store=store, item_id=4106).exists())
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="G3_INITIAL_STOCK").count(), 1)
        self.level()
        self.bag_a.money = 200; self.bag_a.save(update_fields=["money"])
        wood = self.raw(8, item_id=1002, expires_world_minute=None)
        quote = next(t["upgrade"] for t in tools_overview(self.a, self.bag_a, 100)["tools"] if t["item_id"] == 4106)
        self.assertTrue(quote["available"], quote)
        original = self.tool.instance_id
        execute_tool_action(self.a, self.bag_a, "upgrade_tool", dict(instance_id=str(original),
            expected_quote=quote["quote_hash"]), uuid.uuid4(), self.clock)
        self.tool.refresh_from_db(); wood.refresh_from_db(); self.bag_a.refresh_from_db(); account.refresh_from_db()
        self.assertEqual((self.tool.instance_id, self.tool.item_id, self.tool.remaining_uses), (original, 4106, 20))
        self.assertEqual((wood.item_amount, self.bag_a.money, account.balance), (6, 180, balance_before + 20))
