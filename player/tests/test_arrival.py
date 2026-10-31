import copy
import importlib
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase

from player.arrival import arrival_overview
from player.models import PlayerArrivalState, PlayerTutorialState, PlayerSkillProgress
from village.models import EconomicLedgerEntry
from village.player_actions import execute_player_action
from village.tests.test_player_actions import PlayerFixture


class ArrivalTests(PlayerFixture, TestCase):
    def test_new_arrival_is_account_owned_one_shot_and_independent_of_tutorial(self):
        self.assertTrue(arrival_overview(self.a)["should_play"])
        self.run_action("overview")  # Existing account initialization is independent.
        xp_before = list(PlayerSkillProgress.objects.filter(player=self.a).values_list("skill_code", "total_xp"))
        money_before = self.bag_a.money
        ledger_before = EconomicLedgerEntry.objects.count()
        payload = self.payload("arrival_begin")
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        self.assertTrue(first["data"]["result"]["arrival_started_now"])
        replay = execute_player_action(copy.deepcopy(payload))
        self.assertTrue(replay.pop("replayed"))
        self.assertEqual(first, replay)
        self.assertEqual(first["data"]["overview"]["arrival"], dict(version=1,
            revision=1, player_id=str(self.a.pk), eligible=True, started=True,
            finished=False, should_play=False, finish_reason=""))
        # A fresh request after reconnect never starts the opening again.
        self.assertFalse(self.run_action("arrival_begin")["data"]["result"]["arrival_started_now"])
        self.assertTrue(arrival_overview(self.b)["should_play"])
        finished = self.run_action("arrival_finish", {"reason": "skipped"})
        self.assertTrue(finished["data"]["result"]["arrival_finished_now"])
        self.assertEqual(finished["data"]["overview"]["arrival"]["revision"], 2)
        self.assertTrue(self.run_action("overview")["data"]["overview"]["tutorial"]["show_welcome"])
        self.assertFalse(self.run_action("arrival_finish", {"reason": "completed"})
            ["data"]["result"]["arrival_finished_now"])
        state = PlayerArrivalState.objects.get(player=self.a)
        self.assertEqual((state.revision, state.finish_reason), (2, "skipped"))
        self.assertFalse(PlayerTutorialState.objects.filter(player=self.a).exists())
        self.assertEqual(list(PlayerSkillProgress.objects.filter(player=self.a).values_list("skill_code", "total_xp")), xp_before)
        self.bag_a.refresh_from_db()
        self.assertEqual(self.bag_a.money, money_before)
        self.assertEqual(EconomicLedgerEntry.objects.count(), ledger_before)

    def test_arrival_receipts_do_not_initialize_inventory_or_build_gameplay_panels(self):
        with patch("village.player_actions.overview", side_effect=AssertionError("full overview on arrival")), \
                patch("village.player_actions.get_player_main_bag", side_effect=AssertionError("inventory on arrival")):
            for action in ("arrival_begin", "arrival_finish"):
                answer = self.run_action(action)
                self.assertTrue(answer["success"], answer)
                self.assertTrue(answer["data"]["overview_partial"])
                self.assertEqual(set(answer["data"]["overview"]), {"arrival"})

    def test_existing_accounts_are_excluded_and_missing_state_does_not_reenable(self):
        PlayerArrivalState.objects.filter(player=self.a).delete()
        migration = importlib.import_module("player.migrations.0021_playerarrivalstate")
        migration.exclude_existing_accounts(apps, SimpleNamespace(connection=connection))
        self.assertFalse(arrival_overview(self.a)["eligible"])
        # Existing accounts remain excluded even with completely untouched teaching.
        self.assertFalse(PlayerTutorialState.objects.filter(player=self.a).exists())
        begun = self.run_action("arrival_begin")
        self.assertTrue(begun["success"])
        self.assertFalse(begun["data"]["result"]["arrival_started_now"])
        PlayerArrivalState.objects.filter(player=self.a).delete()
        with self.assertNumQueries(1):
            self.assertFalse(arrival_overview(self.a)["should_play"])
        self.assertFalse(PlayerArrivalState.objects.filter(player=self.a).exists())

    def test_rejected_and_rolled_back_actions_do_not_consume_arrival(self):
        denied = self.payload("arrival_begin")
        denied["read_only"] = True
        self.assertEqual(execute_player_action(denied)["result_code"], "MODE_READ_ONLY")
        self.assertEqual(self.run_action("arrival_finish")["result_code"], "ARRIVAL_NOT_STARTED")
        self.assertEqual(self.run_action("arrival_begin", {"xp": 100})["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.run_action("arrival_finish", {"reason": []})["result_code"], "INVALID_PARAMS")
        with patch("village.player_actions.VillagePlayerActionReceipt.objects.create", side_effect=RuntimeError("receipt failed")):
            with self.assertRaises(RuntimeError):
                self.run_action("arrival_begin")
        self.assertTrue(arrival_overview(self.a)["should_play"])
