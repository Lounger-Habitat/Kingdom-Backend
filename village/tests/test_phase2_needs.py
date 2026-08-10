import uuid

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.models import NpcNeedsDaySummary, NpcRuntimeState, VillageLocationDefinition, VillageSimulationConfig
from village.phase2_services import NeedsService, runtime_summary
from village.runtime_services import ResultCode
from village.views import npc_available_actions, npc_runtime_summary


class NeedsServiceTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.create(config_key="default", village_needs_enabled=True)
        VillageLocationDefinition.objects.create(location_code="BILL_HOME", display_name="home", center_anchor_code="home", location_type="home")
        npc = NpcDefinition.objects.create(agent_id="10011", name="bill")
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
