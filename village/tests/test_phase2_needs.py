import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.command_catalog import COMMAND_CATALOG, agent_tool, load_command_catalog
from village.models import NpcNeedsDaySummary, NpcProfile, NpcRuntimeState, VillageLocationDefinition, VillageSimulationConfig
from village.phase2_services import NeedsService, need_bands, runtime_summary
from village.runtime_services import CommandService, ResultCode
from village.views import npc_available_actions, npc_runtime_summary


class NeedsServiceTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.create(config_key="default", village_needs_enabled=True)
        VillageLocationDefinition.objects.create(location_code="BILL_HOME", display_name="home", center_anchor_code="home", location_type="home")
        npc = NpcDefinition.objects.create(agent_id="10011", name="bill")
        NpcProfile.objects.create(
            npc=npc, display_name="Bill", occupation="farmer",
            home_location_code="BILL_HOME", work_location_code="BILL_HOME",
            social_location_code="BILL_HOME",
            capabilities=["EAT", "REST", "SLEEP", "SOCIALIZE", "WORK_PLACEHOLDER"],
        )
        self.runtime = NpcRuntimeState.objects.create(npc=npc, needs_world_minute=0)
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 1500
        time_state.time_revision = 7
        time_state.save(update_fields=["world_minute", "time_revision"])

    def payload(self, *, end=60, version=0, activity="IDLE", tick_id=None):
        return {
            "npc_id": "10011", "advance_to_world_minute": end,
            "expected_state_version": version, "tick_id": str(tick_id or uuid.uuid4()),
            "activity_intervals": [{"interval_id": str(uuid.uuid4()), "start_world_minute": 0, "end_world_minute": end, "activity": activity}],
        }

    def test_fixed_point_idempotency_and_forbidden_absolute_needs(self):
        tick_id = uuid.uuid4()
        payload = self.payload(tick_id=tick_id)
        result = NeedsService.advance(payload)
        self.assertTrue(result["success"])
        self.runtime.refresh_from_db()
        self.assertEqual((self.runtime.hunger_fp, self.runtime.social_fp, self.runtime.needs_world_minute), (240, 220, 60))
        replay = NeedsService.advance(payload)
        self.assertEqual(replay["result_code"], ResultCode.COMMAND_REPLAYED)
        self.runtime.refresh_from_db()
        self.assertEqual(self.runtime.hunger_fp, 240)
        bad = dict(payload, hunger=0)
        self.assertEqual(NeedsService.advance(bad)["result_code"], ResultCode.NEEDS_FIELDS_NOT_ALLOWED)

    def test_social_need_uses_high_values_as_bad_bands(self):
        self.runtime.social_fp = 900
        self.assertEqual(need_bands(self.runtime)["social"], "CRITICAL")
        self.runtime.social_fp = 100
        self.assertEqual(need_bands(self.runtime)["social"], "NORMAL")

    def test_intervals_must_be_contiguous_and_day_summary_finalizes_once(self):
        bad = self.payload(end=60)
        bad["activity_intervals"][0]["start_world_minute"] = 1
        self.assertEqual(NeedsService.advance(bad)["result_code"], ResultCode.INVALID_ACTIVITY_INTERVAL)
        payload = self.payload(end=1440)
        result = NeedsService.advance(payload)
        self.assertTrue(result["success"])
        self.runtime.refresh_from_db()
        # One day of non-severe hunger does not change health without meal/sleep.
        self.assertEqual(self.runtime.health_fp, 1000)

    def test_three_game_day_soak_splits_days_and_applies_severe_hunger_once_per_day(self):
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 3 * 1440
        time_state.save(update_fields=["world_minute"])
        result = NeedsService.advance(self.payload(end=3 * 1440))
        self.assertTrue(result["success"])
        self.runtime.refresh_from_db()
        summaries = list(
            NpcNeedsDaySummary.objects.filter(npc=self.runtime.npc).order_by("day_index")
        )
        self.assertEqual([summary.day_index for summary in summaries], [0, 1, 2])
        self.assertTrue(all(summary.finalized for summary in summaries))
        self.assertLess(summaries[0].severe_hunger_minutes, 1440)
        self.assertEqual([summary.severe_hunger_minutes for summary in summaries[1:]], [1440, 1440])
        self.assertEqual(self.runtime.health_fp, 840)

    def test_oversized_cutover_gap_is_rejected_without_replay(self):
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 3 * 1440 + 1
        time_state.save(update_fields=["world_minute"])

        result = NeedsService.advance(self.payload(end=3 * 1440 + 1))

        self.assertFalse(result["success"])
        self.assertEqual(result["result_code"], ResultCode.STALE_NEEDS_MINUTE)
        self.runtime.refresh_from_db()
        self.assertEqual(self.runtime.needs_world_minute, 0)
        self.assertFalse(NpcNeedsDaySummary.objects.filter(npc=self.runtime.npc).exists())

    def test_frozen_runtime_summary_and_available_actions_http_routes(self):
        user = GameAccount.objects.create(username="phase2-env", password="unused")
        factory = APIRequestFactory()
        request = factory.get("/api/village/npcs/10011/runtime-summary")
        force_authenticate(request, user=user)
        response = npc_runtime_summary(request, "10011")
        self.assertEqual(response.status_code, 200)
        self.assertIn("public_needs", response.data["data"])
        request = factory.get("/api/village/npcs/10011/available-actions")
        force_authenticate(request, user=user)
        response = npc_available_actions(request, "10011")
        self.assertEqual(response.status_code, 200)
        self.assertIn("EAT", response.data["data"]["available_actions"])
        self.assertEqual(response.data["data"]["need_bands"]["hunger"], "NORMAL")
        self.assertEqual(response.data["data"]["needs_rules"]["directions"]["hunger"], "LOWER_IS_BETTER")

    def test_discover_only_returns_configured_actions(self):
        self.runtime.npc.village_profile.capabilities = ["EAT", "MOVE"]
        self.runtime.npc.village_profile.save(update_fields=["capabilities"])
        user = GameAccount.objects.create(username="configured-actions-env", password="unused")
        request = APIRequestFactory().get("/api/village/npcs/10011/available-actions")
        force_authenticate(request, user=user)

        response = npc_available_actions(request, "10011")

        self.assertEqual(
            {spec["name"] for spec in response.data["data"]["action_specs"]},
            {"EAT", "MOVE"},
        )
        self.assertEqual(response.data["data"]["available_actions"], ["EAT", "MOVE"])
        self.assertEqual(
            {spec["expires_world_minute"] for spec in response.data["data"]["action_specs"]},
            {1530},
        )
        self.assertEqual(
            {tool["name"] for tool in response.data["data"]["tools"]},
            {"EAT", "MOVE"},
        )
        for tool in response.data["data"]["tools"]:
            self.assertEqual(set(tool), {"name", "description", "parameters"})
            self.assertEqual(set(tool["parameters"]), {"type", "properties", "required"})

    def test_sleeping_discover_exposes_clock_remaining_time_and_wake_up(self):
        command_id = uuid.uuid4()
        self.runtime.current_action = "SLEEP"
        self.runtime.action_status = "SLEEPING"
        self.runtime.action_started_world_minute = 1500
        self.runtime.action_ends_world_minute = 1980
        self.runtime.active_command_id = command_id
        self.runtime.save()
        user = GameAccount.objects.create(username="sleeping-discover-env", password="unused")
        request = APIRequestFactory().get("/api/village/npcs/10011/discover")
        force_authenticate(request, user=user)

        data = npc_available_actions(request, "10011").data["data"]

        self.assertEqual(data["world_minute"], 1500)
        self.assertEqual(data["action_status"], "SLEEPING")
        self.assertEqual(data["remaining_minutes"], 480)
        self.assertEqual(data["active_command_id"], str(command_id))
        self.assertIn("WAKE_UP", data["available_actions"])

    def test_command_catalog_is_the_allowed_action_source(self):
        self.assertEqual(CommandService.ALLOWED_ACTIONS, frozenset(COMMAND_CATALOG))
        self.assertEqual(len(COMMAND_CATALOG), 45)
        for action in COMMAND_CATALOG:
            self.assertEqual(set(agent_tool(action)), {"name", "description", "parameters"})

    def test_invalid_command_catalog_fails_fast(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "commands.json"
            path.write_text('{"actions": [{"name": "MOVE"}]}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "缺少必要字段"):
                load_command_catalog(path)

    def test_available_actions_apply_health_hunger_and_energy_thresholds(self):
        self.runtime.hunger_fp = 900
        self.runtime.save(update_fields=["hunger_fp"])
        result = runtime_summary("10011")
        self.assertNotIn("WORK_PLACEHOLDER", result["available_actions"])
        self.assertIn("SOCIALIZE", result["available_actions"])

        self.runtime.hunger_fp = 200
        self.runtime.energy_fp = 100
        self.runtime.save(update_fields=["hunger_fp", "energy_fp"])
        result = runtime_summary("10011")
        self.assertNotIn("WORK_PLACEHOLDER", result["available_actions"])

        self.runtime.energy_fp = 800
        self.runtime.health_fp = 300
        self.runtime.save(update_fields=["energy_fp", "health_fp"])
        result = runtime_summary("10011")
        self.assertNotIn("WORK_PLACEHOLDER", result["available_actions"])
        self.assertNotIn("SOCIALIZE", result["available_actions"])
        self.assertTrue({"EAT", "REST", "SLEEP"}.issubset(result["available_actions"]))

    def test_low_health_hides_production_with_stable_reason(self):
        profile = self.runtime.npc.village_profile
        profile.capabilities = [*profile.capabilities, "PLANT"]
        profile.save(update_fields=["capabilities"])
        config = VillageSimulationConfig.objects.get(config_key="default")
        config.village_economy_enabled = True
        config.save(update_fields=["village_economy_enabled"])
        self.runtime.health_fp = 0
        self.runtime.save(update_fields=["health_fp"])

        result = runtime_summary("10011")

        self.assertNotIn("PLANT", result["available_actions"])
        self.assertEqual(result["unavailable_reasons"]["PLANT"], ResultCode.HEALTH_TOO_LOW)
