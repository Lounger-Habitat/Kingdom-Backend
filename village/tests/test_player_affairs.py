import importlib
import uuid
from datetime import timedelta
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from village.affairs import ensure_bridge_affair, complete_labor, normalize_definition, DEFAULT_REQUIREMENTS, project_affair
from village.models import (VillageAffair, VillageAffairContribution, VillagePlayerAffairLabor, VillageWorldEvent,
                            VillagePublicDecision, VillageStoryMeeting, VillageSimulationConfig)
from village.phase3_catalog import REPAIR_WOOD_ITEM_ID
from village.player_actions import execute_player_action
from village.player_affair_labor import tick_player_labor
from village.tests.test_player_actions import PlayerFixture


class PlayerAffairTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.nonce = str(uuid.uuid4())
        self.fact = self.project()
        self.affair = ensure_bridge_affair(self.clock.world_minute)

    def action(self, action, params=None, **kwargs):
        payload = self.payload(action, params, **kwargs)
        payload["authority_session_id"] = self.nonce
        return execute_player_action(payload)

    def ready(self, labor=0):
        self.affair.progress = dict(conditions={"legacy_material_1002": 100, "legacy_meals": 20,
            "legacy_gold": 500, "legacy_labor": labor},
            materials={str(REPAIR_WOOD_ITEM_ID): 100}, meals=20, gold=500, labor=labor)
        self.affair.status = "ACTIVE"
        self.affair.save()

    def start(self, **kwargs):
        result = self.action("labor_start", dict(affair_id=self.affair.pk), location="OLD_BRIDGE", **kwargs)
        self.assertTrue(result["success"], result)
        return result["data"]["result"]

    def tick(self, labor, advance=0, **overrides):
        self.clock.world_minute += advance
        self.clock.time_revision += 1
        self.clock.save()
        payload = dict(player_id=str(self.a.pk), labor_id=labor["labor_id"], authority_session_id=self.nonce,
                       current_location_code="OLD_BRIDGE", present=True, observed_world_minute=self.clock.world_minute)
        payload.update(overrides)
        return tick_player_labor(payload)

    def test_shared_material_meal_gold_progress_without_meeting(self):
        VillagePublicDecision.objects.all().delete()
        VillageStoryMeeting.objects.all().delete()
        self.fact.arc.current_phase = "SHORTAGE_VISIBLE"
        self.fact.arc.current_plan = ""
        self.fact.arc.save()
        self.slot(40, item_id=REPAIR_WOOD_ITEM_ID)
        self.slot(60, item_id=REPAIR_WOOD_ITEM_ID, bag=self.bag_b)
        self.slot(20, item_id=29201)
        self.bag_a.money = 600; self.bag_a.save()
        for player, amount in [(self.a, 40), (self.b, 60)]:
            self.assertTrue(self.action("contribute", dict(affair_id=self.affair.pk, kind="material",
                item_id=REPAIR_WOOD_ITEM_ID, quantity=amount), player=player)["success"])
        self.assertTrue(self.action("contribute", dict(affair_id=self.affair.pk, kind="meal", item_id=29201, quantity=20))["success"])
        self.assertEqual(self.action("labor_start", dict(affair_id=self.affair.pk), location="OLD_BRIDGE")["result_code"],
                         "AFFAIR_PREPARATION_INCOMPLETE")
        rid = uuid.uuid4()
        result = self.action("contribute", dict(affair_id=self.affair.pk, kind="fund", quantity=500), rid=rid)
        self.assertTrue(result["success"], result)
        self.assertEqual(result, self.action("contribute", dict(affair_id=self.affair.pk, kind="fund", quantity=500), rid=rid))
        self.bag_a.refresh_from_db(); self.affair.refresh_from_db()
        self.assertEqual((self.bag_a.money, self.affair.status, self.affair.progress["labor"]), (100, "ACTIVE", 0))
        self.start()

    def test_money_and_bad_food_fail_without_progress_or_consumption(self):
        result = self.action("contribute", dict(affair_id=self.affair.pk, kind="fund", quantity=500))
        self.assertEqual(result["result_code"], "INSUFFICIENT_MONEY")
        self.slot(2, item_id=29201, expires_world_minute=100)
        result = self.action("contribute", dict(affair_id=self.affair.pk, kind="meal", item_id=29201, quantity=1))
        self.assertEqual(result["result_code"], "INSUFFICIENT_QUALIFIED_ITEMS")
        self.assertEqual(VillageAffairContribution.objects.filter(affair=self.affair).count(), 0)

    def test_bridge_deliveries_require_yard_and_labor_requires_bridge(self):
        self.slot(2, item_id=REPAIR_WOOD_ITEM_ID)
        self.slot(2, item_id=29201, quality=3)
        for params in (dict(kind="material", item_id=REPAIR_WOOD_ITEM_ID, quantity=1),
                       dict(kind="meal", item_id=29201, quantity=1), dict(kind="fund", quantity=1)):
            params["affair_id"] = self.affair.pk
            self.assertEqual(self.action("contribute", params, location="OLD_BRIDGE")["result_code"], "LOCATION_INVALID")
            result = self.action("contribute", params, location="REPAIR_YARD")
            self.assertTrue(result["success"], result)
        self.assertEqual(VillageAffairContribution.objects.filter(affair=self.affair).count(), 3)
        self.ready()
        self.assertEqual(self.action("labor_start", dict(affair_id=self.affair.pk), location="REPAIR_YARD")["result_code"],
                         "LOCATION_INVALID")
        labor = self.start()
        self.tick(labor, 30)
        result = self.tick(labor, 30)
        self.assertEqual(result["data"]["credited_units"], 1)
        result = self.tick(labor, 1, current_location_code="REPAIR_YARD")
        self.assertEqual(result["data"]["labor"]["stop_reason"], "LEFT_WORKSITE")
        self.affair.refresh_from_db()
        self.assertEqual(self.affair.progress["labor"], 1)

    def test_bridge_worksite_migration_preserves_progress_and_completed_state(self):
        migration = importlib.import_module("village.migrations.0062_bridge_worksites")
        self.ready(labor=3)
        progress = self.affair.progress
        self.affair.requirements = normalize_definition(DEFAULT_REQUIREMENTS, self.affair.locations, validate_references=False)
        self.affair.save(update_fields=["requirements"])
        sessions = [VillagePlayerAffairLabor.objects.create(player=player, affair=self.affair,
            authority_session_id=uuid.uuid4(), location_code=place, accumulated_minutes=20, completed_units=1,
            last_world_minute=100, last_time_revision=1, last_seen_at=timezone.now())
            for player, place in ((self.a, "REPAIR_YARD"), (self.b, "OLD_BRIDGE"))]
        migration.separate_worksites(apps, SimpleNamespace(connection=connection))
        self.affair.refresh_from_db()
        self.assertEqual(self.affair.progress, progress)
        self.assertEqual([s["locations"] for s in project_affair(self.affair)["stages"]], [["REPAIR_YARD"], ["OLD_BRIDGE"]])
        for session in sessions:
            session.refresh_from_db()
        self.assertEqual((sessions[0].status, sessions[0].accumulated_minutes, sessions[0].completed_units), ("CANCELLED", 0, 1))
        self.assertEqual((sessions[1].status, sessions[1].accumulated_minutes), ("ACTIVE", 20))
        version = self.affair.version
        migration.separate_worksites(apps, SimpleNamespace(connection=connection))
        self.affair.refresh_from_db()
        self.assertEqual(self.affair.version, version)
        self.ready(labor=20)
        self.affair.status = "COMPLETED"
        self.affair.completed_world_minute = 1300
        self.affair.requirements = normalize_definition(DEFAULT_REQUIREMENTS, self.affair.locations, validate_references=False)
        self.affair.save()
        migration.separate_worksites(apps, SimpleNamespace(connection=connection))
        self.affair.refresh_from_db()
        self.assertEqual((self.affair.status, self.affair.completed_world_minute, self.affair.progress["labor"]),
                         ("COMPLETED", 1300, 20))

    def test_sixty_real_game_minutes_and_retry_complete_only_once(self):
        self.ready(labor=19)
        labor = self.start()
        self.tick(labor, 30)
        self.tick(labor, 29)
        self.affair.refresh_from_db(); self.assertEqual(self.affair.progress["labor"], 19)
        final = self.tick(labor, 1)
        self.assertEqual(final["data"]["credited_units"], 1)
        self.assertEqual(self.tick(labor)["data"]["credited_units"], 0)
        self.fact.refresh_from_db(); self.affair.refresh_from_db()
        self.assertEqual((self.affair.status, self.fact.bridge_condition), ("COMPLETED", "REPAIRED"))
        self.assertEqual(VillageAffairContribution.objects.filter(affair=self.affair, kind="labor").count(), 1)
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="VILLAGE_AFFAIR_COMPLETED").count(), 1)

    def test_cancel_discards_partial_unit_and_same_start_receipt_does_not_credit(self):
        self.ready()
        rid = uuid.uuid4()
        labor = self.start(rid=rid)
        self.tick(labor, 30)
        cancel = self.action("labor_cancel")
        self.assertTrue(cancel["success"], cancel)
        self.start(rid=rid)  # Historical response does not mutate the stopped row.
        result = self.tick(labor, 30)
        self.assertEqual(result["data"]["labor"]["status"], "CANCELLED")
        self.affair.refresh_from_db(); self.assertEqual(self.affair.progress["labor"], 0)

    def test_leaving_or_losing_presence_cancels_unfinished_unit(self):
        self.ready()
        for overrides in [dict(present=False), dict(current_location_code="VILLAGE_SQUARE")]:
            labor = self.start()
            self.tick(labor, 30)
            result = self.tick(labor, 29, **overrides)
            self.assertEqual(result["data"]["labor"]["status"], "CANCELLED")
            self.assertEqual(result["data"]["labor"]["accumulated_minutes"], 0)
        self.affair.refresh_from_db(); self.assertEqual(self.affair.progress["labor"], 0)

    def test_replayed_start_reports_live_progress_then_terminal_labor_without_resurrection(self):
        self.ready(labor=19)
        rid = uuid.uuid4()
        labor = self.start(rid=rid)
        self.tick(labor, 30)
        params = dict(affair_id=self.affair.pk)
        replay = self.action("labor_start", params, location="OLD_BRIDGE", rid=rid)
        self.assertEqual(replay["data"]["receipt_labor"]["accumulated_minutes"], 30)
        self.assertEqual(replay["data"]["current_labor"]["status"], "ACTIVE")
        self.tick(labor, 30)
        replay = self.action("labor_start", params, location="OLD_BRIDGE", rid=rid)
        self.assertIsNone(replay["data"]["current_labor"])
        self.assertEqual(replay["data"]["receipt_labor"]["status"], "COMPLETED")
        self.assertEqual(replay["data"]["receipt_labor"]["completed_units"], 1)
        self.assertEqual(VillageAffairContribution.objects.filter(affair=self.affair, kind="labor").count(), 1)

    def test_cancel_replay_does_not_cancel_a_later_labor_session(self):
        self.ready()
        first = self.start()
        rid = uuid.uuid4()
        self.action("labor_cancel", rid=rid)
        second = self.start()
        replay = self.action("labor_cancel", rid=rid)
        self.assertEqual(replay["data"]["receipt_labor"]["labor_id"], first["labor_id"])
        self.assertEqual(replay["data"]["receipt_labor"]["status"], "CANCELLED")
        self.assertEqual(replay["data"]["current_labor"]["labor_id"], second["labor_id"])
        self.assertEqual(replay["data"]["current_labor"]["status"], "ACTIVE")

    def test_lease_expiry_and_clock_jump_never_credit_offline_time(self):
        self.ready()
        labor = self.start()
        VillagePlayerAffairLabor.objects.filter(pk=labor["labor_id"]).update(last_seen_at=timezone.now()-timedelta(seconds=11))
        self.assertEqual(self.tick(labor, 30)["data"]["labor"]["stop_reason"], "PRESENCE_EXPIRED")
        labor = self.start()
        self.assertEqual(self.tick(labor, 60)["data"]["labor"]["stop_reason"], "CLOCK_DISCONTINUITY")
        self.affair.refresh_from_db(); self.assertEqual(self.affair.progress["labor"], 0)

    def test_reconnect_new_nonce_restarts_at_zero_and_rejects_old_ticks(self):
        self.ready()
        first = self.start(); self.tick(first, 30)
        self.nonce = str(uuid.uuid4())
        second = self.start()
        self.assertNotEqual(first["labor_id"], second["labor_id"])
        self.assertEqual(second["accumulated_minutes"], 0)
        self.assertEqual(self.tick(first)["result_code"], "LABOR_SESSION_INVALID")

    def test_start_request_ids_are_namespaced_by_player(self):
        self.ready()
        rid = uuid.uuid4()
        first = self.start(rid=rid)
        second = self.start(rid=rid, player=self.b)
        self.assertNotEqual(first["labor_id"], second["labor_id"])
        self.assertEqual(VillagePlayerAffairLabor.objects.filter(status="ACTIVE").count(), 2)

    def test_client_cannot_tick_finish_report_time_or_call_env_endpoint(self):
        self.ready(); labor = self.start()
        self.assertEqual(self.action("labor_tick")["result_code"], "INVALID_PARAMS")
        self.assertEqual(self.action("labor_start", dict(affair_id=self.affair.pk, duration_minutes=60))["result_code"], "INVALID_PARAMS")
        client = APIClient(); client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(self.a)}")
        self.assertEqual(client.post("/api/village/player/labor/tick/", {}, format="json").status_code, 401)
        self.assertEqual(self.tick(labor, player_id=str(self.b.pk))["result_code"], "LABOR_SESSION_INVALID")

    def test_delayed_tick_never_credits_time_after_its_observation(self):
        self.ready(); labor = self.start()
        result = self.tick(labor, 30, observed_world_minute=105)
        self.assertEqual(result["data"]["labor"]["accumulated_minutes"], 5)

    def test_other_worker_last_unit_terminates_without_extra_credit(self):
        self.ready(labor=19); labor = self.start(); self.tick(labor, 30)
        complete_labor(self.affair, world_minute=self.clock.world_minute, request_id=uuid.uuid4(), player_id=str(self.b.pk))
        result = self.tick(labor, 30)
        self.assertEqual(result["data"]["credited_units"], 0)
        self.affair.refresh_from_db(); self.assertEqual(self.affair.progress["labor"], 20)

    def test_second_affair_reuses_player_contribution_and_labor(self):
        self.affair = VillageAffair.objects.create(code="FIX_WELL", title="修井", kind="RESOURCE_PROJECT",
            requirements=dict(materials={}, meals=0, gold=5, labor=1), progress={}, locations=["OLD_BRIDGE"], opened_world_minute=100)
        result = self.action("contribute", dict(affair_id="FIX_WELL", kind="fund", quantity=5), location="OLD_BRIDGE")
        self.assertTrue(result["success"], result)
        labor = self.start(); self.tick(labor, 30); self.tick(labor, 30)
        self.affair.refresh_from_db(); self.assertEqual(self.affair.status, "COMPLETED")
        self.fact.refresh_from_db(); self.assertNotEqual(self.fact.bridge_condition, "REPAIRED")

    def test_feature_flags_stop_active_labor_and_reject_new_contributions(self):
        self.ready(); labor = self.start(); self.tick(labor, 30)
        VillageSimulationConfig.objects.filter(config_key="default").update(village_story_enabled=False)
        self.assertEqual(self.tick(labor, 30)["data"]["labor"]["stop_reason"], "FEATURE_DISABLED")
        self.assertEqual(self.action("labor_start", dict(affair_id=self.affair.pk), location="OLD_BRIDGE")["result_code"], "FEATURE_DISABLED")
        self.assertEqual(self.action("contribute", dict(affair_id=self.affair.pk, kind="fund", quantity=1))["result_code"], "FEATURE_DISABLED")

    def test_non_resource_affair_and_untrusted_start_context_are_rejected(self):
        VillageAffair.objects.create(code="MEETING_TEST", kind="MEETING", title="讨论", opened_world_minute=100)
        self.assertEqual(self.action("contribute", dict(affair_id="MEETING_TEST", kind="fund", quantity=1))["result_code"], "AFFAIR_NOT_FOUND")
        self.ready()
        for flag in ["read_only", "replay_only"]:
            payload = self.payload("labor_start", dict(affair_id=self.affair.pk), location="OLD_BRIDGE")
            payload["authority_session_id"] = self.nonce; payload[flag] = True
            self.assertFalse(execute_player_action(payload)["success"])
        self.assertFalse(VillagePlayerAffairLabor.objects.exists())
