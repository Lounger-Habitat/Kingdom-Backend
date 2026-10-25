import copy
import uuid
from unittest.mock import patch

from django.db import transaction, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from player.models import PlayerTutorialState, PlayerMilestoneProgress, PlayerSkillProgress
from player.progression import award_skill
from player.tutorials import control, observe_action, observe_skill, observe_weapon, tutorial_overview, TutorialError
from player.tutorial_catalog import VERSION, ROUTES, TASKS
from village.tests.test_player_actions import PlayerFixture
from village.models import EconomicLedgerEntry


class TutorialTests(PlayerFixture, TestCase):
    def start(self, route="basics"):
        result = self.run_action("tutorial_track", {"route_code": route})
        self.assertTrue(result["success"], result)
        return result["data"]["overview"]["tutorial"]

    def project(self, player=None):
        player = player or self.a
        bag = self.bag_a if player == self.a else self.bag_b
        return tutorial_overview(player, bag, self.clock.world_minute,
            dict(player_id=str(player.pk), starter={}), read_only=True)

    def ack(self, hint, task=None):
        task = task or self.project()["current_task"]["code"]
        return self.run_action("tutorial_ack", {"hint_code": hint, "task_code": task})

    def activate(self, code):
        # Seed prerequisites; each test then exercises the real recording path.
        row = TASKS[code]
        state, _ = PlayerTutorialState.objects.get_or_create(player=self.a,
            defaults=dict(version=VERSION, started=True))
        state.version, state.started, state.route_code = VERSION, True, row["route_code"]
        for earlier in ROUTES[row["route_code"]][:row["index"] - 1]:
            state.completed.setdefault(earlier["code"], "fixture")
        state.save()
        return state

    def test_catalog_has_basics_and_six_independent_chains_with_stable_codes(self):
        self.assertEqual(len(ROUTES), 7)
        self.assertEqual(sum(len(rows) for rows in ROUTES.values()), len(TASKS))
        self.assertTrue(all(3 <= len(rows) <= 6 for rows in ROUTES.values()))
        self.assertTrue(all(len(code) <= 50 for code in TASKS))
        self.assertFalse(any(row["condition"][0] == "mastery" for row in TASKS.values()))
        self.assertEqual([row["condition"] for row in TASKS.values() if row["condition"][0] == "level"],
                         [("level", "gathering", 2)])

    def test_browsing_and_observation_never_create_progress_or_award_assets(self):
        with CaptureQueriesContext(connection) as queries:
            state = self.project()
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertFalse(PlayerTutorialState.objects.exists())
        self.assertFalse(state["can_write"])
        denied = self.payload("tutorial_start"); denied["read_only"] = True
        self.assertEqual(self.run_action("overview")["success"], True)
        from village.player_actions import execute_player_action
        self.assertEqual(execute_player_action(denied)["result_code"], "MODE_READ_ONLY")

    def test_routes_switch_preserves_progress_and_pause_survives_reconnect(self):
        self.start()
        for hint in ("movement", "camera"):
            self.assertTrue(self.ack(hint)["success"])
        self.start("cooking")
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("basics_move", state.completed)
        self.assertEqual(state.route_code, "cooking")
        self.run_action("tutorial_pause")
        self.assertTrue(self.project()["paused"])
        self.assertFalse(self.project(self.b)["started"])
        self.start("basics")
        self.assertFalse(self.project()["paused"])
        self.assertEqual(self.project()["current_task"]["code"], "basics_collect")

    def test_client_cannot_acknowledge_gameplay_or_forge_experience(self):
        self.start("cooking")
        before = self.bag_a.money
        for hint in ("cook", "income", "sale", "cooking_first"):
            result = self.ack(hint)
            self.assertEqual(result["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.run_action("tutorial_track", {"route_code": []})["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.run_action("tutorial_ack", {"hint_code": {}})["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.run_action("tutorial_track", {"route_code": "fake"})["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.run_action("tutorial_ack", {"hint_code": "recipe", "xp": 100})["result_code"], "INVALID_PARAMS")
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, before)
        self.assertFalse(EconomicLedgerEntry.objects.filter(entry_type="STARTER_GOAL_REWARD").exists())

    def test_paused_current_task_records_once_without_advancing_other_routes(self):
        self.activate("cooking_first")
        self.run_action("tutorial_pause")
        for _ in range(2):
            award_skill(self.a, source_type="player_cooking", source_id="one-cook", skill_code="cooking", xp=10,
                        facts={"recipe_id": 1})
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("cooking_first", state.completed)
        self.assertNotIn("gathering_use", state.completed)
        self.assertEqual(state.evidence["cooking_first"]["cook"], ["player_cooking:one-cook"])
        self.assertNotIn("cooking_again", state.completed)
        award_skill(self.a, source_type="player_cooking", source_id="second-cook", skill_code="cooking", xp=10, facts={})
        state.refresh_from_db(); self.assertNotIn("cooking_again", state.completed)

    def test_async_stall_sale_advances_from_skill_settlement_without_player_request(self):
        self.activate("business_sale")
        award_skill(self.a, source_type="stall_sale", source_id="customer-event", skill_code="business", xp=5,
                    facts={"transaction_id": 1})
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("business_sale", state.completed)
        self.assertNotIn("basics_income", state.completed)
        self.assertEqual(self.project()["current_task"]["code"], "business_accounts")

    def test_first_cook_completes_preparation_when_preview_acknowledgement_is_late(self):
        self.activate("cooking_prepare")
        award_skill(self.a, source_type="player_cooking", source_id="first-dish", skill_code="cooking", xp=10)
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("cooking_prepare", state.completed)
        self.assertIn("cooking_first", state.completed)
        self.assertEqual(state.evidence["cooking_first"]["cook"], ["player_cooking:first-dish"])
        self.assertEqual(self.project()["current_task"]["code"], "cooking_review")
        self.assertNotIn("cooking_review", state.completed)
        # The delayed UI acknowledgement cannot change the now-current review.
        self.assertFalse(self.ack("cooking_preview", "cooking_prepare")["data"]["result"]["tutorial_acknowledged"])
        award_skill(self.a, source_type="dish_review", source_id="first-review", skill_code="cooking", xp=1)
        self.assertEqual(self.project()["current_task"]["code"], "cooking_read")

    def test_cooking_preparation_shortcut_does_not_apply_to_other_routes_or_rollback(self):
        self.activate("cooking_recipe")
        award_skill(self.a, source_type="player_cooking", source_id="before-recipe", skill_code="cooking", xp=1)
        self.assertEqual(self.project()["current_task"]["code"], "cooking_recipe")
        state = self.activate("cooking_prepare")
        with self.assertRaises(RuntimeError), transaction.atomic():
            award_skill(self.a, source_type="player_cooking", source_id="failed-dish", skill_code="cooking", xp=1)
            raise RuntimeError("settlement failed")
        state.refresh_from_db()
        self.assertNotIn("cooking_prepare", state.completed)
        self.assertNotIn("cooking_first", state.completed)
        self.assertEqual(state.evidence, {})

    def test_cook_owned_by_another_route_cannot_complete_cooking_preparation(self):
        self.activate("gathering_use")
        award_skill(self.a, source_type="player_cooking", source_id="other-route", skill_code="cooking", xp=1)
        self.activate("cooking_prepare")
        observe_skill(self.a, "player_cooking", "other-route", {})
        self.assertEqual(self.project()["current_task"]["code"], "cooking_prepare")

    def test_rollback_removes_tutorial_evidence_with_failed_settlement(self):
        self.activate("cooking_first")
        with self.assertRaises(RuntimeError), transaction.atomic():
            award_skill(self.a, source_type="player_cooking", source_id="rollback-cook", skill_code="cooking", xp=10)
            raise RuntimeError("settlement failed")
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertNotIn("cooking_first", state.completed)
        self.assertEqual(state.evidence, {})

    def test_historical_facts_are_not_imported_into_new_tasks(self):
        award_skill(self.a, source_type="player_cooking", source_id="old-one", skill_code="cooking", xp=10)
        award_skill(self.a, source_type="player_cooking", source_id="old-two", skill_code="cooking", xp=10)
        self.assertFalse(PlayerTutorialState.objects.exists())
        self.start("cooking")
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertEqual((state.evidence, state.completed), ({}, {}))
        self.ack("recipe"); self.ack("cooking_preview")
        self.assertEqual(self.project()["current_task"]["code"], "cooking_first")
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="cooking").total_xp, 20)

    def test_replayed_control_returns_saved_receipt_and_cannot_reset_route(self):
        payload = self.payload("tutorial_track", {"route_code": "cooking"})
        from village.player_actions import execute_player_action
        first = execute_player_action(payload)
        self.start("business")
        self.assertEqual(first, execute_player_action(copy.deepcopy(payload)))
        self.assertEqual(PlayerTutorialState.objects.get(player=self.a).route_code, "business")

    def test_route_selection_completes_only_its_unlocked_basics_step(self):
        self.activate("basics_route")
        self.start("gathering")
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("basics_route", state.completed)
        self.assertEqual(state.evidence["basics_route"], {"route": ["gathering"]})
        self.assertEqual(self.project()["current_task"]["code"], "gathering_first")

    def test_task_completion_and_evidence_are_bounded(self):
        self.activate("business_list")
        for _ in range(5):
            observe_action(self.a, self.bag_a, "list_stall_dish", {"quantity": 1}, uuid.uuid4())
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertEqual(len(state.evidence["business_list"]["list"]), 1)
        self.assertNotIn("business_restock", state.completed)
        self.assertEqual(set(state.evidence), {"business_list"})

    def test_business_teaches_opening_before_listing(self):
        self.start("business"); self.ack("business")
        self.assertEqual(self.project()["current_task"]["code"], "business_open")
        result = self.run_action("open_player_stall", location="PLAYER_STALL")
        self.assertTrue(result["success"], result)
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("business_open", state.completed)
        self.assertNotIn("business_list", state.completed)
        self.assertEqual(self.project()["current_task"]["code"], "business_list")

    def test_existing_stall_can_continue_without_opening_it_again(self):
        from restaurant.models import PlayerRestaurantState
        PlayerRestaurantState.objects.create(player=self.a, stall_opened_world_minute=50)
        self.start("business"); self.ack("business")
        self.assertEqual(self.project()["current_task"]["code"], "business_list")

    def test_harvest_and_sowing_do_not_complete_the_renewal_lesson(self):
        self.activate("farming_harvest")
        award_skill(self.a, source_type="farm_harvest", source_id="harvest-one", skill_code="farming", xp=20)
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("farming_harvest", state.completed)
        self.assertNotIn("farming_next", state.completed)
        observe_action(self.a, self.bag_a, "plant_farm_plot", {}, "sow-two")
        state.refresh_from_db(); self.assertNotIn("farming_next", state.completed)
        observe_action(self.a, self.bag_a, "renew_farm_plot", {}, "renew-once")
        state.refresh_from_db(); self.assertIn("farming_next", state.completed)

    def test_income_before_step_six_does_not_complete_or_prefill_it(self):
        self.start("gathering")
        observe_action(self.a, self.bag_a, "sell_store", {"gold_amount": 10}, "early-sale")
        award_skill(self.a, source_type="player_cooking", source_id="early-cook", skill_code="cooking", xp=10)
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertEqual(state.evidence, {})
        self.assertNotIn("gathering_use", state.completed)
        route = next(row for row in self.project()["routes"] if row["code"] == "gathering")
        self.assertEqual(route["tasks"][5]["status"], "LOCKED")
        self.activate("gathering_use")
        self.assertFalse(self.project()["current_task"]["completed"])
        observe_action(self.a, self.bag_a, "sell_store", {"gold_amount": 5}, "new-sale")
        state.refresh_from_db()
        self.assertIn("gathering_use", state.completed)
        self.assertEqual(state.evidence["gathering_use"], {"income": ["new-sale"]})
        self.assertNotIn("basics_income", state.completed)
        self.assertNotIn("hunting_use", state.completed)

    def test_each_collection_belongs_only_to_the_current_task(self):
        self.start("gathering")
        award_skill(self.a, source_type="collect", source_id="first", skill_code="gathering", xp=1,
                    facts={"tool_item_id": 123})
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("gathering_first", state.completed)
        self.assertNotIn("basics_collect", state.completed)
        self.assertNotIn("gathering_equipped", state.completed)
        self.assertEqual(set(state.evidence), {"gathering_first"})
        self.activate("gathering_equipped")
        observe_skill(self.a, "collect", "first", {"facts": {"tool_item_id": 123}})
        self.assertEqual(self.project()["current_task"]["code"], "gathering_equipped")
        award_skill(self.a, source_type="collect", source_id="equipped", skill_code="gathering", xp=1,
                    facts={"tool_item_id": 123})
        self.assertEqual(self.project()["current_task"]["code"], "gathering_recipe")
        self.assertEqual(self.project()["current_task"]["learning_hints"], ["recipe"])
        self.ack("recipe")
        self.assertEqual(self.project()["current_task"]["code"], "gathering_use")
        award_skill(self.a, source_type="collect", source_id="recipe-collect", skill_code="gathering", xp=1)
        self.assertEqual(self.project()["current_task"]["code"], "gathering_use")

    def test_another_routes_cooking_task_requires_its_own_new_cook(self):
        self.activate("cooking_first")
        award_skill(self.a, source_type="player_cooking", source_id="first", skill_code="cooking", xp=10)
        self.activate("gathering_use")
        observe_skill(self.a, "player_cooking", "first", {})
        self.assertEqual(self.project()["current_task"]["code"], "gathering_use")
        award_skill(self.a, source_type="player_cooking", source_id="second", skill_code="cooking", xp=10)
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("gathering_use", state.completed)
        self.assertEqual(state.evidence["gathering_use"]["cook"], ["player_cooking:second"])

    def test_partial_learning_is_preserved_per_task_across_route_switches(self):
        self.start(); self.ack("movement")
        self.start("cooking"); self.ack("recipe")
        self.assertEqual(self.project()["current_task"]["code"], "cooking_prepare")
        self.start("basics"); self.ack("camera")
        self.assertEqual(self.project()["current_task"]["code"], "basics_collect")
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertEqual(state.evidence["basics_move"], {"hint:movement": ["read"], "hint:camera": ["read"]})
        self.assertEqual(state.evidence["cooking_recipe"], {"hint:recipe": ["read"]})

    def test_stale_hint_cannot_acknowledge_another_task_with_the_same_hint(self):
        self.start("cooking"); self.ack("recipe")
        self.activate("gathering_recipe")
        result = self.ack("recipe", "cooking_recipe")
        self.assertFalse(result["data"]["result"]["tutorial_acknowledged"])
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertNotIn("gathering_recipe", state.evidence)
        self.ack("recipe")
        state.refresh_from_db(); self.assertEqual(state.evidence["gathering_recipe"], {"hint:recipe": ["read"]})

    def test_unrelated_hint_cannot_prefill_a_locked_task(self):
        self.start("cooking")
        result = self.ack("review", "cooking_read")
        self.assertFalse(result["data"]["result"]["tutorial_acknowledged"])
        self.assertEqual(PlayerTutorialState.objects.get(player=self.a).evidence, {})
        self.assertEqual(self.run_action("tutorial_ack", {"hint_code": "recipe"})["result_code"], "INVALID_PARAMS")

    def test_legacy_progress_discards_completions_after_a_gap_without_browsing_writes(self):
        old = PlayerTutorialState.objects.create(player=self.a, version=1, started=True, route_code="gathering",
            completed={"basics_move": "old", "gathering_first": "old", "gathering_use": "old"},
            evidence={"collect": ["old-collect"], "income": ["old-sale"]})
        with CaptureQueriesContext(connection) as queries:
            view = self.project()
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        route = next(row for row in view["routes"] if row["code"] == "gathering")
        self.assertEqual(route["completed_count"], 1)
        self.assertEqual(route["tasks"][5]["status"], "LOCKED")
        old.refresh_from_db(); self.assertIn("gathering_use", old.completed)
        self.run_action("tutorial_pause")
        old.refresh_from_db()
        self.assertEqual(old.version, VERSION)
        self.assertEqual(old.evidence, {})
        self.assertEqual(old.completed, {"basics_move": "old", "gathering_first": "old"})
        self.assertEqual(old.revision, 1)

    def test_levels_wait_for_prerequisites_without_requiring_reearned_xp(self):
        award_skill(self.a, source_type="collect", source_id="old", skill_code="gathering", xp=1000)
        self.start("gathering")
        self.assertEqual(self.project()["current_task"]["code"], "gathering_first")
        award_skill(self.a, source_type="collect", source_id="new", skill_code="gathering", xp=1)
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertIn("gathering_level2", state.completed)
        self.assertNotIn("gathering_advanced", state.completed)

    def test_restock_teaches_withdrawal_without_repeating_listing(self):
        self.activate("business_restock")
        observe_action(self.a, self.bag_a, "list_stall_dish", {}, "another-list")
        self.assertEqual(self.project()["current_task"]["code"], "business_restock")
        observe_action(self.a, self.bag_a, "withdraw_stall_dish", {}, "withdraw-once")
        self.assertTrue(self.project()["route_completed"])

    def test_routes_end_after_the_last_feature_instead_of_assigning_level_grinds(self):
        for route, rows in ROUTES.items():
            with self.subTest(route=route):
                state = self.activate(rows[-1]["code"])
                state.completed[rows[-1]["code"]] = "fixture"
                state.save()
                view = self.project()
                self.assertIsNone(view["current_task"])
                self.assertTrue(view["route_completed"])
                projected = next(row for row in view["routes"] if row["code"] == route)
                self.assertTrue(projected["intro_completed"])
                self.assertEqual(projected["completed_count"], projected["total_count"])

    def test_v2_upgrade_preserves_partial_work_and_retires_long_term_steps_without_read_writes(self):
        completed = {row["code"]: "old" for row in ROUTES["gathering"][:4]}
        completed["gathering_advanced"] = "old"
        old = PlayerTutorialState.objects.create(player=self.a, version=2, started=True, route_code="gathering",
            completed=completed, evidence={"gathering_recipe": {"hint:recipe": ["read"]},
                "basics_move": {"hint:movement": ["read"]}, "gathering_advanced": {"collect": ["old"]}})
        with CaptureQueriesContext(connection) as queries:
            view = self.project()
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(view["current_task"]["code"], "gathering_use")
        old.refresh_from_db(); self.assertEqual(old.version, 2)
        self.run_action("tutorial_pause")
        old.refresh_from_db()
        self.assertEqual(old.version, VERSION)
        self.assertIn("gathering_recipe", old.completed)
        self.assertNotIn("gathering_advanced", old.completed)
        self.assertNotIn("gathering_advanced", old.evidence)
        self.assertEqual(old.evidence["basics_move"], {"hint:movement": ["read"]})

    def test_v2_account_waiting_at_removed_forestry_step_has_finished_that_route(self):
        PlayerTutorialState.objects.create(player=self.a, version=2, started=True, route_code="forestry",
            completed={row["code"]: "old" for row in ROUTES["forestry"]} | {"forestry_more": "old"},
            evidence={"forestry_growth": {"wood": ["old"]}})
        view = self.project()
        self.assertTrue(view["route_completed"])
        self.assertIsNone(view["current_task"])
        route = next(row for row in view["routes"] if row["code"] == "forestry")
        self.assertEqual(route["total_count"], 3)
        self.assertTrue(route["intro_completed"])

    def test_forestry_goes_from_felling_to_maintenance_without_a_wood_demand_lesson(self):
        self.start("forestry")
        observe_weapon(self.a, "axe", "tutorial-swing")
        self.assertEqual(self.project()["current_task"]["code"], "forestry_first")
        award_skill(self.a, source_type="forestry", source_id="tutorial-tree", skill_code="forestry", xp=10)
        view = self.project()
        self.assertEqual(view["current_task"]["code"], "forestry_maintenance")
        self.assertEqual(view["current_task"]["index"], 3)
        self.ack("equipment")
        view = self.project()
        self.assertTrue(view["route_completed"])
        route = next(row for row in view["routes"] if row["code"] == "forestry")
        self.assertEqual([row["code"] for row in route["tasks"]],
                         ["forestry_equipment", "forestry_first", "forestry_maintenance"])
        self.assertEqual(route["completed_count"], 3)

    def test_v3_forestry_progress_skips_the_removed_step_and_preserves_remaining_work(self):
        for uses_done, maintenance_done in ((False, False), (True, False), (True, True)):
            with self.subTest(uses_done=uses_done, maintenance_done=maintenance_done):
                completed = dict(forestry_equipment="old", forestry_first="old")
                if uses_done:
                    completed["forestry_uses"] = "old"
                if maintenance_done:
                    completed["forestry_maintenance"] = "old"
                old, _ = PlayerTutorialState.objects.update_or_create(player=self.a, defaults=dict(
                    version=3, started=True, paused=False, route_code="forestry", completed=completed,
                    evidence={"forestry_uses": {"hint:wood_uses": ["read"]},
                              "basics_move": {"hint:movement": ["read"]}}))
                with CaptureQueriesContext(connection) as queries:
                    view = self.project()
                self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
                route = next(row for row in view["routes"] if row["code"] == "forestry")
                self.assertEqual(route["total_count"], 3)
                self.assertEqual([row["index"] for row in route["tasks"]], [1, 2, 3])
                self.assertEqual(route["completed_count"], 3 if maintenance_done else 2)
                self.assertEqual(view["current_task"]["code"] if view["current_task"] else None,
                                 None if maintenance_done else "forestry_maintenance")
                old.refresh_from_db()
                self.assertEqual(old.version, 3, "Browsing must not write progress migrations.")
                self.run_action("tutorial_pause")
                old.refresh_from_db()
                self.assertEqual(old.version, VERSION)
                self.assertEqual(old.completed, {code: stamp for code, stamp in completed.items() if code != "forestry_uses"})
                self.assertEqual(old.evidence, {"basics_move": {"hint:movement": ["read"]}})

    def test_pausing_before_start_dismisses_welcome_without_completing_tasks(self):
        result = self.run_action("tutorial_pause")
        self.assertTrue(result["success"])
        state = PlayerTutorialState.objects.get(player=self.a)
        self.assertFalse(state.started)
        self.assertTrue(state.paused)
        self.assertEqual(state.completed, {})
        self.assertFalse(result["data"]["overview"]["tutorial"]["show_welcome"])

    def test_hunting_attack_and_defense_lessons_require_their_own_results(self):
        for code in ("hunting_training", "hunting_defense"):
            self.assertEqual(TASKS[code]["location_code"], "COMBAT_TRAINING")
        self.activate("hunting_training")
        self.assertFalse(self.ack("training_block")["data"]["result"]["tutorial_acknowledged"])
        self.ack("training")
        self.assertEqual(self.project()["current_task"]["code"], "hunting_defense")
        self.assertFalse(self.ack("training")["data"]["result"]["tutorial_acknowledged"])
        self.assertEqual(self.ack("defense")["result_code"], "INVALID_PARAMS")
        self.ack("training_block")
        view = self.project()
        self.assertEqual(view["current_task"]["code"], "hunting_defense")
        self.assertEqual(view["current_task"]["learning_hints"], ["training_dodge"])
        self.assertIn("成功闪避", view["current_task"]["progress_text"])
        self.start("cooking")
        self.start("hunting")
        self.assertEqual(self.project()["current_task"]["learning_hints"], ["training_dodge"])
        self.ack("training_dodge")
        self.assertEqual(self.project()["current_task"]["code"], "hunting_first")

    def test_v4_hunting_preserves_finished_lessons_and_updates_pending_defense_requirements(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                completed = dict(hunting_equipment="old", hunting_training="old")
                if finished:
                    completed["hunting_defense"] = "old"
                PlayerTutorialState.objects.update_or_create(player=self.a, defaults=dict(
                    version=4, started=True, route_code="hunting", completed=completed, evidence={}))
                view = self.project()
                self.assertEqual(view["schema_version"], VERSION)
                self.assertEqual(view["current_task"]["code"], "hunting_first" if finished else "hunting_defense")
                if not finished:
                    self.assertEqual(view["current_task"]["learning_hints"], ["training_block", "training_dodge"])

    def test_automatic_initial_equipment_does_not_suppress_first_welcome(self):
        for code in ("initial_sword_granted", "initial_axe_granted"):
            PlayerMilestoneProgress.objects.create(player=self.a, code=code, title="初始装备",
                source_type="initial_equipment", source_id=str(self.a.pk))
        state = tutorial_overview(self.a, self.bag_a, 100, {"player_id": str(self.a.pk)}, read_only=False)
        self.assertTrue(state["show_welcome"])
        award_skill(self.a, source_type="collect", source_id="real-gather", skill_code="gathering", xp=10)
        state = tutorial_overview(self.a, self.bag_a, 100, {"player_id": str(self.a.pk)}, read_only=False)
        self.assertFalse(state["show_welcome"])
