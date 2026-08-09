from datetime import date

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from game_time.services import TimeAuthorityService
from game_time import views as game_time_views
from village import views as village_views


class TimeAuthorityProductionEntryTests(TestCase):
    """Protect the single CAS authority boundary at actual HTTP view entries."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.env_account = GameAccount.create_user("time-authority-env", "test")
        self.state = GlobalGameTimeState.get_instance()
        self.state.apply_world_minute(1000)
        self.state.time_revision = 7
        self.state.save()

    def _as_env(self, request):
        # force_authenticate deliberately reaches the production permission
        # boundary; token scope validation is covered by account tests.
        force_authenticate(request, user=self.env_account)
        return request

    def test_village_runtime_cas_updates_calendar_projection_and_revision(self):
        request = self._as_env(self.factory.put(
            "/api/village/runtime/time",
            {
                "world_minute": 1001,
                "expected_time_revision": 7,
                "request_id": "1d683218-2f16-4bb3-98df-7e0082294a2d",
            },
            format="json",
        ))

        response = village_views.runtime_time(request)

        self.assertEqual(response.status_code, 200)
        self.state.refresh_from_db()
        self.assertEqual(self.state.world_minute, 1001)
        self.assertEqual(self.state.time_revision, 8)
        projected = date.fromordinal(1001 // 1440 + 1)
        self.assertEqual(
            (self.state.game_year, self.state.game_month, self.state.game_day,
             self.state.game_hour, self.state.game_minute),
            (projected.year, projected.month, projected.day, 16, 41),
        )
        self.assertIs(TimeAuthorityService.snapshot(self.state), self.state)

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
            self.assertEqual(response.data["data"]["time_revision"], before[1])
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
