import uuid
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from village.affairs import (create_resource_affair, complete_labor, project_affair,
                            _material_usage, legacy_definition)
from village.models import VillageWorldEvent
from village.phase4_visibility import public_story_event_fields
from village.player_affair_labor import start_labor, tick_player_labor
from village.player_actions import execute_player_action
from village.tests.test_player_actions import PlayerFixture


class LaborProgressProjectionTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.affair = create_resource_affair("WORK_SYNC", "共享劳动", {"stages": [
            dict(id="work", conditions=[dict(id="build", type="labor", target=8, unit_minutes=1)])
        ]}, [], self.clock.world_minute)
        self.nonce = str(uuid.uuid4())
        self.labor = start_labor(self.a, self.affair, uuid.uuid4(), "", self.clock, self.nonce, "build")

    def tick(self, advance=1):
        self.clock.world_minute += advance
        self.clock.time_revision += 1
        self.clock.save()
        return tick_player_labor(dict(player_id=str(self.a.pk), labor_id=self.labor["labor_id"],
            authority_session_id=self.nonce, current_location_code="", present=True,
            observed_world_minute=self.clock.world_minute))

    def test_tick_reuses_one_projection_and_includes_global_player_and_npc_work(self):
        complete_labor(self.affair, condition_id="build", world_minute=100,
                       request_id=uuid.uuid4(), player_id=str(self.b.pk))
        complete_labor(self.affair, condition_id="build", world_minute=100,
                       request_id=uuid.uuid4(), actor_npc=self.npc)
        with patch("village.affairs.project_affair", wraps=project_affair) as project, \
                patch("village.player_actions.overview", side_effect=AssertionError("full overview")), \
                patch("village.daily_events.evaluate_daily_events"), CaptureQueriesContext(connection) as queries:
            result = self.tick()
        self.assertTrue(result["success"], result)
        self.assertEqual(project.call_count, 1, "Event and tick share the reducer's existing global projection")
        data = result["data"]
        self.assertEqual(data["credited_units"], 1)
        self.assertEqual(data["labor"]["completed_units"], 1)
        self.assertEqual(data["affair"]["progress"]["conditions"]["build"], 3)
        self.assertEqual(data["affair"]["version"], 3)
        self.assertFalse(data["stage_changed"])
        self.assertNotIn("overview", data)
        event = VillageWorldEvent.objects.filter(event_type="VILLAGE_AFFAIR_PROGRESS").latest("event_sequence")
        public = public_story_event_fields(event)
        self.assertEqual(public["affair"], data["affair"])
        self.assertEqual(public["affair"]["labor"]["submitted"], 3)
        self.assertNotIn("authority_session_id", str(public))
        self.assertLess(len(queries), 50)
        print(f"LaborProgressProjection: ordinary credited tick SQL={len(queries)-1} (excludes test clock save)")

    def test_repeated_observation_has_no_delta_or_projection_query(self):
        first = self.tick()
        with patch("village.affairs.project_affair", side_effect=AssertionError("unexpected projection")):
            duplicate = self.tick(0)
        self.assertTrue(duplicate["success"], duplicate)
        self.assertEqual(duplicate["data"]["credited_units"], 0)
        self.assertIsNone(duplicate["data"]["affair"])
        self.assertEqual(duplicate["data"]["labor"]["completed_units"], first["data"]["labor"]["completed_units"])

    def test_bridge_stock_uses_global_labor_without_mutating_submitted(self):
        stages = legacy_definition(dict(materials={"1002": 100}, meals=20, gold=500, labor=20))["stages"]
        progress = dict(legacy_material_1002=100, legacy_meals=20, legacy_gold=500, legacy_labor=1)
        with self.assertNumQueries(0):
            usage = _material_usage("BRIDGE_REPAIR", stages, progress)
        self.assertEqual(usage["legacy_material_1002"], dict(used_quantity=5, stock_quantity=95))
        self.assertEqual(usage["legacy_meals"], dict(used_quantity=1, stock_quantity=19))
        self.assertEqual(progress["legacy_material_1002"], 100)
        self.assertEqual(usage["legacy_gold"], dict(used_quantity=25, stock_quantity=475))
        progress["legacy_labor"] = 20
        self.assertEqual(_material_usage("BRIDGE_REPAIR", stages, progress)["legacy_meals"]["stock_quantity"], 0)

    def test_integer_stock_distribution_and_unmapped_flexible_affairs(self):
        stages = legacy_definition(dict(materials={"1002": 5}, meals=0, gold=0, labor=3))["stages"]
        remaining = []
        for completed in range(4):
            usage = _material_usage("BRIDGE_REPAIR", stages, dict(legacy_material_1002=5, legacy_labor=completed))
            remaining.append(usage["legacy_material_1002"]["stock_quantity"])
        self.assertEqual(remaining, [5, 4, 2, 0])
        custom = _material_usage("CUSTOM", stages, dict(legacy_material_1002=5, legacy_labor=3))
        self.assertEqual(custom["legacy_material_1002"]["stock_quantity"], 5)

    def test_projection_keeps_material_fields_in_historical_completed_stage(self):
        self.affair.code = "BRIDGE_REPAIR"
        self.affair.requirements = legacy_definition(dict(materials={"1002": 100}, meals=0, gold=0, labor=20))
        self.affair.progress = dict(conditions=dict(legacy_material_1002=100, legacy_labor=2))
        projection = project_affair(self.affair)
        material = projection["stages"][0]["conditions"][0]
        self.assertEqual((material["submitted"], material["used_quantity"], material["stock_quantity"]), (100, 10, 90))
        self.assertEqual(projection["materials"][0]["stock_quantity"], 90)

    def test_known_canal_and_expansion_stock_is_derived_without_queries(self):
        canal = [dict(conditions=[dict(id="canal_wood", type="item", target=30)]),
                 dict(conditions=[dict(id="water_labor", type="labor", target=1)])]
        expansion = [dict(conditions=[dict(id="leveling_wood", type="item", target=2),
                                     dict(id="leveling_labor", type="labor", target=1)])]
        with self.assertNumQueries(0):
            water = _material_usage("FARM_IRRIGATION", canal, dict(canal_wood=30, water_labor=1))
            plot = _material_usage("FARM_EXPANSION_E_PLOT", expansion, dict(leveling_wood=1, leveling_labor=1))
            career = _material_usage("CAREER_JACK_1_PREP", [
                dict(conditions=[dict(id="wood", type="item", target=20)]),
                dict(conditions=[dict(id="labor", type="labor", target=3)])], dict(wood=20, labor=1))
        self.assertEqual(career["wood"], dict(used_quantity=6, stock_quantity=14))
        self.assertEqual(water["canal_wood"], dict(used_quantity=30, stock_quantity=0))
        self.assertEqual(plot["leveling_wood"], dict(used_quantity=1, stock_quantity=0))

    def test_start_cancel_keep_their_small_control_paths(self):
        from player.progression import ensure_growth
        from village.player_affair_labor import cancel_labor
        cancel_labor(self.a)
        ensure_growth(self.a)
        payload = self.payload("labor_start", dict(affair_id=self.affair.pk, condition_id="build"))
        payload["authority_session_id"] = self.nonce
        with patch("village.player_actions.overview", side_effect=AssertionError("full overview")), \
                CaptureQueriesContext(connection) as start_queries:
            started = execute_player_action(payload)
        self.assertTrue(started["success"], started)
        payload = self.payload("labor_cancel", dict(labor_id=started["data"]["result"]["labor_id"]))
        payload["authority_session_id"] = self.nonce
        with patch("village.player_actions.overview", side_effect=AssertionError("full overview")), \
                CaptureQueriesContext(connection) as cancel_queries:
            stopped = execute_player_action(payload)
        self.assertTrue(stopped["success"], stopped)
        self.assertLess(len(start_queries), 20)
        self.assertLess(len(cancel_queries), 15)
        print(f"LaborProgressProjection: control SQL start={len(start_queries)} cancel={len(cancel_queries)}")
