"""Checkpoint-driven passive time persistence (Unity is the time authority).

All global time writes flow through ``trusted_persistence.save_snapshots`` /
``save_result`` → ``TimeService.receive_time``, which is monotonic: forwards
save (with calendar projection), same-minute is idempotent, regression is
rejected.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from game_time import views as game_time_views
from inventory.models import NpcDefinition
from village.models import NpcRuntimeState
from village.trusted_persistence import save_result, save_snapshots


def _checkpoint(world_minute, time_revision, state_version=1, location="RESTAURANT"):
    return {
        "world_minute": world_minute,
        "time_revision": time_revision,
        "checkpoints": [{
            "npc_id": "10011",
            "current_location_code": location,
            "current_point_code": location,
            "state_version": state_version,
            "public_needs": {"hunger": 12.5, "energy": 77.1},
        }],
    }


class CheckpointTimePersistenceTests(TestCase):
    def setUp(self):
        self.state = GlobalGameTimeState.get_instance()
        self.state.world_minute = 1000
        self.state.time_revision = 7
        self.state.save()
        NpcDefinition.objects.create(agent_id="10011", name="bill", is_test_only=False)

    def test_advancing_time_saves_and_projects_calendar(self):
        result = save_snapshots(_checkpoint(1001, 8))
        self.assertTrue(result["success"])
        self.state.refresh_from_db()
        self.assertEqual((self.state.world_minute, self.state.time_revision), (1001, 8))
        projected = date.fromordinal(1001 // 1440 + 1)
        self.assertEqual(
            (self.state.game_year, self.state.game_month, self.state.game_day),
            (projected.year, projected.month, projected.day),
        )
        self.assertEqual((self.state.game_hour, self.state.game_minute), divmod(1001 % 1440, 60))

    def test_same_minute_duplicate_is_idempotent(self):
        payload = _checkpoint(1000, 9)
        self.assertTrue(save_snapshots(payload)["success"])
        self.assertTrue(save_snapshots(payload)["success"])
        self.state.refresh_from_db()
        # 同分钟不改 revision。
        self.assertEqual((self.state.world_minute, self.state.time_revision), (1000, 7))

    def test_old_checkpoint_returns_world_time_regression(self):
        result = save_snapshots(_checkpoint(999, 8))
        self.assertFalse(result["success"])
        self.assertEqual(result["result_code"], "WORLD_TIME_REGRESSION")
        self.state.refresh_from_db()
        self.assertEqual((self.state.world_minute, self.state.time_revision), (1000, 7))

    def test_non_monotonic_revision_is_rejected(self):
        # 分钟前进但 revision 未前进：拒绝。
        result = save_snapshots(_checkpoint(1001, 7))
        self.assertFalse(result["success"])
        self.assertEqual(result["result_code"], "STALE_TIME_REVISION")
        self.state.refresh_from_db()
        self.assertEqual(self.state.world_minute, 1000)

    def test_old_checkpoint_does_not_overwrite_newer_npc_snapshot(self):
        NpcRuntimeState.objects.create(
            npc=NpcDefinition.objects.get(agent_id="10011"),
            state_version=5,
            current_location_code="NEW_LOCATION",
        )
        result = save_snapshots(_checkpoint(1001, 8, state_version=3, location="OLD_LOCATION"))
        self.assertTrue(result["success"])
        self.assertTrue(result["results"][0]["skipped"])
        runtime = NpcRuntimeState.objects.get(npc__agent_id="10011")
        self.assertEqual(runtime.current_location_code, "NEW_LOCATION")
        self.assertEqual(runtime.state_version, 5)

    def test_delayed_action_result_saves_but_does_not_regress_global_time(self):
        save_snapshots(_checkpoint(1001, 8))
        result = save_result({
            "command_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "npc_id": "10011",
            "environment_id": "WuxiVillage",
            "action": "MOVE",
            "success": True,
            "result_code": "MOVE_COMPLETED",
            "world_minute": 500,
            "time_revision": 3,
            "state": {"state_version": 6, "current_location_code": "OLD_LOCATION"},
        })
        self.assertTrue(result["success"])
        self.state.refresh_from_db()
        # 延迟结果仍保存为历史，但其旧时间不回退全局时间。
        self.assertEqual((self.state.world_minute, self.state.time_revision), (1001, 8))


class ConcurrentCheckpointTimeTests(TransactionTestCase):
    """并发 checkpoint 最终保留最大时间（仅 PostgreSQL 生效）。"""

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("requires config.settings_postgres_test")
        self.state = GlobalGameTimeState.get_instance()
        self.state.world_minute = 1000
        self.state.time_revision = 7
        self.state.save()
        NpcDefinition.objects.create(agent_id="10011", name="bill", is_test_only=False)

    def test_concurrent_checkpoints_keep_max_time(self):
        def submit(minute, revision):
            return save_snapshots(_checkpoint(minute, revision))

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(submit, 1002, 10)
            f2 = pool.submit(submit, 1005, 13)
            f1.result()
            f2.result()
        self.state.refresh_from_db()
        self.assertEqual(self.state.world_minute, 1005)
        self.assertEqual(self.state.time_revision, 13)


class PlayerTimeEndpointFailClosedTests(TestCase):
    """General player-facing game-time endpoints stay fail-closed on mutation."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.env_account = GameAccount.create_user("time-authority-env", "test")
        self.state = GlobalGameTimeState.get_instance()
        self.state.apply_world_minute(1000)
        self.state.time_revision = 7
        self.state.save()

    def _as_env(self, request):
        force_authenticate(request, user=self.env_account)
        return request

    def test_legacy_env_mutation_endpoints_fail_closed_without_mutating_cas_state(self):
        before = (self.state.world_minute, self.state.time_revision)
        for method, view in (("put", game_time_views.global_time_update),
                             ("post", game_time_views.global_time_day_advanced)):
            request = self._as_env(getattr(self.factory, method)(
                "/game/time/global/update",
                {
                    "game_year": 9999,
                    "game_month": 12,
                    "game_day": 31,
                    "game_hour": 23,
                    "game_minute": 59,
                },
                format="json",
            ))
            response = view(request)
            self.assertEqual(response.status_code, 409)
            self.assertFalse(response.data["success"])
            self.assertEqual(response.data["data"]["world_minute"], before[0])
        self.state.refresh_from_db()
        self.assertEqual((self.state.world_minute, self.state.time_revision), before)

    def test_legacy_player_endpoints_are_snapshot_or_rejection_only(self):
        before = (self.state.world_minute, self.state.time_revision)
        sync = game_time_views.time_sync(self._as_env(self.factory.post(
            "/game/time/sync", {"game_hour": 23, "game_minute": 59}, format="json"
        )))
        rejected = game_time_views.day_advanced(self._as_env(self.factory.post(
            "/game/time/day-advanced", {"game_year": 9999}, format="json"
        )))

        self.assertTrue(sync.data["success"])
        self.assertFalse(rejected.data["success"])
        self.state.refresh_from_db()
        self.assertEqual((self.state.world_minute, self.state.time_revision), before)
