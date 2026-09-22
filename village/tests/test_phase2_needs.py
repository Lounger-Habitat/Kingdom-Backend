import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.command_catalog import COMMAND_CATALOG, agent_tool, load_command_catalog
from village.models import NpcActionReservation, NpcNeedsDaySummary, NpcProfile, NpcRuntimeState, VillageActionReceipt, VillageLocationDefinition, VillageSimulationConfig
from village.phase2_services import NeedsService, TickService, need_bands, runtime_summary, settle_action_needs_locked
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
            capabilities=["EAT", "REST", "SLEEP", "WORK_PLACEHOLDER"],
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
        # Two all-day severe-hunger penalties plus three core nights with no sleep.
        self.assertEqual(self.runtime.health_fp, 690)

    def test_sleep_curve_rewards_one_continuous_block(self):
        self.assertEqual(
            [NeedsService._sleep_curve_percent(minutes) for minutes in (0, 120, 240, 360, 480, 600, 720)],
            [0, 6, 25, 56, 100, 110, 110],
        )

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

    def test_tick_rolls_back_time_and_all_needs_when_one_npc_fails(self):
        other_npc = NpcDefinition.objects.get(agent_id="10012")
        other, _ = NpcRuntimeState.objects.get_or_create(npc=other_npc)
        other.needs_world_minute = 1500
        other.save(update_fields=["needs_world_minute"])
        self.runtime.needs_world_minute = 1500
        self.runtime.save(update_fields=["needs_world_minute"])
        time_state = GlobalGameTimeState.get_instance()
        tick_id = uuid.uuid4()
        requests = []
        for npc_id, version in (("10011", 0), ("10012", 99)):
            requests.append({
                "npc_id": npc_id, "advance_to_world_minute": 1501,
                "expected_state_version": version, "tick_id": str(uuid.uuid4()),
                "activity_intervals": [{
                    "interval_id": str(uuid.uuid4()), "start_world_minute": 1500,
                    "end_world_minute": 1501, "activity": "IDLE",
                }],
            })

        result = TickService.advance({
            "world_minute": 1501, "time_revision": 8,
            "request_id": str(tick_id), "needs_requests": requests,
        })

        self.assertFalse(result["success"])
        time_state.refresh_from_db(); self.runtime.refresh_from_db(); other.refresh_from_db()
        self.assertEqual((time_state.world_minute, time_state.time_revision), (1500, 7))
        self.assertEqual((self.runtime.needs_world_minute, other.needs_world_minute), (1500, 1500))
        self.assertFalse(VillageActionReceipt.objects.filter(request_id=tick_id).exists())

    def test_tick_commits_and_replays_as_one_idempotent_unit(self):
        self.runtime.needs_world_minute = 1500
        self.runtime.save(update_fields=["needs_world_minute"])
        payload = {
            "world_minute": 1501, "time_revision": 8,
            "request_id": str(uuid.uuid4()),
            "needs_requests": [{
                "npc_id": "10011", "advance_to_world_minute": 1501,
                "expected_state_version": 0, "tick_id": str(uuid.uuid4()),
                "activity_intervals": [{
                    "interval_id": str(uuid.uuid4()), "start_world_minute": 1500,
                    "end_world_minute": 1501, "activity": "IDLE",
                }],
            }],
        }

        result = TickService.advance(payload)
        replay = TickService.advance(payload)

        self.assertTrue(result["success"])
        self.assertEqual(replay["result_code"], ResultCode.COMMAND_REPLAYED)
        self.runtime.refresh_from_db()
        time_state = GlobalGameTimeState.get_instance()
        self.assertEqual((time_state.world_minute, time_state.time_revision), (1501, 8))
        self.assertEqual(self.runtime.needs_world_minute, 1501)

    def test_tick_applies_base_to_busy_npc_without_client_needs_batch(self):
        self.runtime.needs_world_minute = 1500
        self.runtime.action_status = "ACTING"
        self.runtime.save(update_fields=["needs_world_minute", "action_status"])
        reservation = NpcActionReservation.objects.create(
            action_kind="DIALOGUE", active_lock_key="test:tick:busy",
            acquired_world_minute=1500, expires_world_minute=1600,
        )
        reservation.participants.set([self.runtime.npc])
        result = TickService.advance({
            "world_minute": 1501, "time_revision": 8,
            "request_id": str(uuid.uuid4()),
        })
        self.assertTrue(result["success"], result)
        self.runtime.refresh_from_db()
        self.assertEqual(self.runtime.needs_world_minute, 1501)
        self.assertEqual((self.runtime.hunger_remainder, self.runtime.social_remainder), (2000, 1000))

    def test_tick_accepts_jump_and_replays_same_minute_without_reapplying(self):
        self.runtime.needs_world_minute = 1500
        self.runtime.save(update_fields=["needs_world_minute"])
        payload = {"world_minute": 1560, "time_revision": 8, "request_id": str(uuid.uuid4())}
        result = TickService.advance(payload)
        self.assertTrue(result["success"], result)
        self.runtime.refresh_from_db()
        hunger = self.runtime.hunger_fp
        replay = TickService.advance({**payload, "request_id": str(uuid.uuid4())})
        self.assertEqual(replay["result_code"], ResultCode.COMMAND_REPLAYED)
        self.runtime.refresh_from_db()
        self.assertEqual((self.runtime.needs_world_minute, self.runtime.hunger_fp), (1560, hunger))
        old = TickService.advance({"world_minute": 1559, "time_revision": 9, "request_id": str(uuid.uuid4())})
        self.assertFalse(old["success"])
        self.assertEqual(GlobalGameTimeState.get_instance().world_minute, 1560)

    def test_action_delta_maps_move_and_economy_work_on_top_of_base(self):
        self.assertIsNone(settle_action_needs_locked(self.runtime, "MOVE", 0, 30, base_until=30))
        self.runtime.save()
        self.runtime.refresh_from_db()
        self.assertEqual(
            (self.runtime.hunger_fp, self.runtime.energy_fp,
             self.runtime.social_fp, self.runtime.stress_fp),
            (220, 790, 210, 105),
        )
        self.runtime.hunger_fp, self.runtime.energy_fp = 200, 800
        self.runtime.social_fp, self.runtime.stress_fp = 200, 100
        self.runtime.needs_world_minute = 0
        self.runtime.save(update_fields=[
            "hunger_fp", "energy_fp", "social_fp", "stress_fp", "needs_world_minute",
        ])
        self.assertIsNone(settle_action_needs_locked(self.runtime, "HUNT", 0, 30, base_until=30))
        self.runtime.save()
        self.runtime.refresh_from_db()
        self.assertEqual(
            (self.runtime.hunger_fp, self.runtime.energy_fp,
             self.runtime.social_fp, self.runtime.stress_fp),
            (230, 760, 210, 120),
        )

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
            {"GET_RULE", "EAT", "MOVE"},
        )
        self.assertEqual(response.data["data"]["available_actions"], ["EAT", "GET_RULE", "MOVE"])
        self.assertEqual(
            {spec["expires_world_minute"] for spec in response.data["data"]["action_specs"]},
            {1530},
        )
        self.assertEqual(
            {tool["name"] for tool in response.data["data"]["tools"]},
            {"GET_RULE", "EAT", "MOVE"},
        )
        for tool in response.data["data"]["tools"]:
            self.assertEqual(set(tool), {"name", "description", "parameters", "tags"})
            self.assertTrue({"type", "properties", "required"}.issubset(tool["parameters"]))

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
        self.assertIn("PROCESS_GAME", COMMAND_CATALOG)
        for action in COMMAND_CATALOG:
            tool = agent_tool(action)
            self.assertEqual(set(tool), {"name", "description", "parameters", "tags"})
            if action in CommandService.READ_QUERY_ACTIONS:
                self.assertEqual(tool["tags"], ["read"], action)
            else:
                self.assertEqual(tool["tags"], [], action)
        # 阶段三/四的这两个查询同样只读，必须携带 read 标签。
        for query in ("INSPECT_STORE_INVENTORY", "INSPECT_SOCIAL_STATE"):
            self.assertEqual(agent_tool(query)["tags"], ["read"], query)

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
        self.assertNotIn("START_DIALOGUE", result["available_actions"])
        self.assertNotIn("SPEAK", result["available_actions"])

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
        self.assertNotIn("START_DIALOGUE", result["available_actions"])
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
