"""Phase 3 economy API tests using real Django test client.

Tests hit actual URL routes -- never call views directly.
"""

import uuid

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from village.models import (
    DailyPriceSnapshot,
    NpcCommandState,
    NpcRuntimeState,
    VillageAccount,
    VillageActionReceipt,
    VillageFarmPlot,
    VillageRestaurantState,
    VillageSimulationConfig,
    VillageWorldEvent,
)
from village.phase3_contract import (
    VILLAGE_ECONOMY_ACTION_V1,
    FarmPlotStatus,
    ResultCode,
)


FORMAL_NPC_IDS = ("10011", "10012", "10013", "10014", "10015")


def _economy_action_payload(**overrides):
    """Build a valid economy action payload."""
    base = {
        "schema_version": VILLAGE_ECONOMY_ACTION_V1,
        "command_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "command_name": "PLANT",
        "npc_id": 10011,
        "completed_world_minute": 600,
        "time_revision": 600,
        "current_location_code": "BILL_FARM",
        "params": {
            "plot_code": "FARM_PLOT_A",
            "crop_code": "CORN",
            "expected_asset_version": 0,
        },
    }
    base.update(overrides)
    if "params" in overrides and "params" not in base:
        base["params"] = overrides["params"]
    return base


class Phase3EconomyApiTests(TestCase):
    """Real URL tests for Phase 3 economy endpoints."""

    def setUp(self):
        # Create service account for auth
        self.service_account = GameAccount.objects.create(
            username=f"phase3-env-{uuid.uuid4().hex[:12]}", password="unused"
        )
        self.client = APIClient()

        # Create NPCs and inventories
        self.npcs = {}
        self.inventories = {}
        for npc_id in FORMAL_NPC_IDS:
            npc, _ = NpcDefinition.objects.get_or_create(
                agent_id=int(npc_id),
                defaults={"name": f"npc-{npc_id}", "is_test_only": False},
            )
            self.npcs[npc_id] = npc
            inv, _ = NpcGlobalInventory.objects.get_or_create(
                npc=npc, defaults={"money": 200, "asset_version": 1}
            )
            self.inventories[npc_id] = inv
            # Production commands require the runtime row that Unity seeds
            # via checkpoints; the service rejects otherwise (Unity authority).
            NpcRuntimeState.objects.get_or_create(npc=npc)

        # Enable economy
        self.config, _ = VillageSimulationConfig.objects.get_or_create(
            config_key="default",
            defaults={"village_economy_enabled": True},
        )
        self.config.village_economy_enabled = True
        self.config.save(update_fields=["village_economy_enabled"])

        # Create time state
        self.time_state = GlobalGameTimeState.get_instance()
        self.time_state.world_minute = 10000
        self.time_state.time_revision = 0
        self.time_state.save()

        # Create farm plots
        for code in ("FARM_PLOT_A", "FARM_PLOT_B", "FARM_PLOT_C"):
            VillageFarmPlot.objects.get_or_create(
                plot_code=code, defaults={"status": "EMPTY"}
            )

        # Create public fund
        self.public_fund, _ = VillageAccount.objects.get_or_create(
            account_code="PUBLIC_FUND",
            defaults={"balance": 100, "asset_version": 1},
        )

        # Create restaurant state
        VillageRestaurantState.objects.get_or_create(
            day_index=0, defaults={"status": "OPEN"}
        )

    def _auth_header(self):
        """Return Authorization header for env server."""
        token = AccessToken.for_user(self.service_account)
        token["scope"] = "env_server"
        token["aud"] = "kingdom-env"
        return f"Bearer {token}"

    # ── actions/complete tests ────────────────────────────────────────────────

    def test_rate_dish_invalid_instance_id_returns_structured_200(self):
        payload = _economy_action_payload(
            command_name="RATE_DISH",
            npc_id=10013,
            current_location_code="RECORD_HALL",
            params={
                "owner_npc_id": 10014,
                "food_instance_id": "1",
                "score": 4,
            },
        )

        resp = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_complete_plant_success(self):
        """Valid PLANT action returns 200 with OK."""
        # Give Bill corn seeds
        NpcGlobalInventorySlot.objects.create(
            bag=self.inventories["10011"],
            slot_index=0,
            item_id=1051,  # corn seed
            item_amount=5,
            remaining_uses=None,
        )
        # Give Bill farm tool
        NpcGlobalInventorySlot.objects.create(
            bag=self.inventories["10011"],
            slot_index=1,
            item_id=4001,  # farm tool
            item_amount=1,
            remaining_uses=20,
        )

        payload = _economy_action_payload(
            command_name="PLANT",
            npc_id=10011,
            completed_world_minute=600,
            time_revision=600,
            current_location_code="BILL_FARM",
            params={
                "plot_code": "FARM_PLOT_A",
                "crop_code": "CORN",
                "expected_asset_version": 1,
            },
        )

        resp = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["result_code"], ResultCode.OK)
        self.assertFalse(data["replayed"])
        self.assertIn("effects", data)

    def test_complete_feature_disabled(self):
        """Economy flag OFF returns 200 with P3_FEATURE_DISABLED."""
        self.config.village_economy_enabled = False
        self.config.save(update_fields=["village_economy_enabled"])

        payload = _economy_action_payload()
        resp = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["result_code"], ResultCode.P3_FEATURE_DISABLED)

    def test_complete_wrong_actor(self):
        """Wrong NPC returns 200 with P3_FORBIDDEN_ACTOR."""
        payload = _economy_action_payload(
            command_name="PLANT",
            npc_id=10012,  # Jack, not Bill
            current_location_code="BILL_FARM",
        )
        resp = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["result_code"], ResultCode.P3_FORBIDDEN_ACTOR)

    def test_complete_replay(self):
        """Same request_id + same payload returns replayed=true."""
        # Give Bill corn seeds and farm tool so first call succeeds
        NpcGlobalInventorySlot.objects.create(
            bag=self.inventories["10011"],
            slot_index=0,
            item_id=1051,
            item_amount=5,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=self.inventories["10011"],
            slot_index=1,
            item_id=4001,
            item_amount=1,
            remaining_uses=20,
        )

        payload = _economy_action_payload(
            command_name="PLANT",
            npc_id=10011,
            completed_world_minute=600,
            time_revision=600,
            current_location_code="BILL_FARM",
            params={
                "plot_code": "FARM_PLOT_A",
                "crop_code": "CORN",
                "expected_asset_version": 1,
            },
        )

        # First call
        resp1 = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.json()["result_code"], ResultCode.OK)

        # Replay
        resp2 = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp2.status_code, 200)
        data = resp2.json()
        self.assertTrue(data["replayed"])

    def test_complete_conflict(self):
        """Same request_id + different payload returns 409."""
        # Give Bill corn seeds and farm tool so first call succeeds
        NpcGlobalInventorySlot.objects.create(
            bag=self.inventories["10011"],
            slot_index=0,
            item_id=1051,
            item_amount=5,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=self.inventories["10011"],
            slot_index=1,
            item_id=4001,
            item_amount=1,
            remaining_uses=20,
        )

        request_id = str(uuid.uuid4())
        payload1 = _economy_action_payload(
            request_id=request_id,
            npc_id=10011,
            command_name="PLANT",
            completed_world_minute=600,
            time_revision=600,
            current_location_code="BILL_FARM",
            params={
                "plot_code": "FARM_PLOT_A",
                "crop_code": "CORN",
                "expected_asset_version": 1,
            },
        )
        payload2 = _economy_action_payload(
            request_id=request_id,
            npc_id=10011,
            command_name="HUNT",  # different command
            current_location_code="HUNTING_GATE",
        )

        # First call -- must succeed to store receipt
        resp1 = self.client.post(
            "/api/village/economy/actions/complete",
            payload1,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.json()["result_code"], ResultCode.OK)

        # Conflict
        resp = self.client.post(
            "/api/village/economy/actions/complete",
            payload2,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual(data["result_code"], ResultCode.P3_REQUEST_CONFLICT)

    def test_complete_unauthenticated(self):
        """No auth returns 401."""
        payload = _economy_action_payload()
        resp = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_complete_rejects_unknown_fields(self):
        """Unknown top-level field returns 400."""
        payload = _economy_action_payload()
        payload["unknown_field"] = "bad"
        resp = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertEqual(data["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_complete_rejects_missing_required_field(self):
        """Missing required field returns 400."""
        payload = _economy_action_payload()
        del payload["command_name"]
        resp = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertEqual(data["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_complete_rejects_invalid_schema_version(self):
        """Invalid schema_version returns 400."""
        payload = _economy_action_payload(schema_version="wrong.v1")
        resp = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_complete_rejects_invalid_uuid(self):
        """Invalid UUID returns 400."""
        payload = _economy_action_payload(request_id="not-a-uuid")
        resp = self.client.post(
            "/api/village/economy/actions/complete",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 400)

    # ── snapshot tests ────────────────────────────────────────────────────────

    def test_snapshot_success(self):
        """GET returns 200 with all required fields."""
        resp = self.client.get(
            "/api/village/economy/snapshot",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        inner = data["data"]
        self.assertIn("world_minute", inner)
        self.assertIn("last_closed_day_index", inner)
        self.assertIn("farm_plots", inner)
        self.assertIn("prices", inner)
        self.assertIn("restaurant_status", inner)
        self.assertIn("store_status", inner)
        self.assertIn("open_orders", inner)
        self.assertIn("active_deliveries", inner)
        self.assertIn("public_fund_balance", inner)
        self.assertIn("npc_asset_versions", inner)
        self.assertEqual(len(inner["farm_plots"]), 36)
        self.assertEqual(len(inner["allowed_plot_codes"]), 36)
        self.assertEqual(
            inner["allowed_crop_codes"], ["CORN", "WHEAT", "CARROT", "POTATO"]
        )
        self.assertIn("batch_id", inner["farm_plots"][0])

    def test_snapshot_mature_projection(self):
        """GROWING plot with current time >= mature is reported as MATURE."""
        plot = VillageFarmPlot.objects.get(plot_code="FARM_PLOT_A")
        plot.status = "GROWING"
        plot.crop_code = "CORN"
        plot.planted_world_minute = 500
        plot.mature_world_minute = 3380  # already past current 10000
        plot.save()

        resp = self.client.get(
            "/api/village/economy/snapshot",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        plots = {p["plot_code"]: p for p in data["data"]["farm_plots"]}
        self.assertEqual(plots["FARM_PLOT_A"]["status"], "MATURE")

    def test_snapshot_unauthenticated(self):
        """No auth returns 401."""
        resp = self.client.get("/api/village/economy/snapshot")
        self.assertEqual(resp.status_code, 401)

    def test_snapshot_read_only(self):
        """Snapshot does not modify farm plots."""
        plot = VillageFarmPlot.objects.get(plot_code="FARM_PLOT_A")
        plot.status = "GROWING"
        plot.crop_code = "CORN"
        plot.planted_world_minute = 500
        plot.mature_world_minute = 20000
        plot.save()

        self.client.get(
            "/api/village/economy/snapshot",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        plot.refresh_from_db()
        self.assertEqual(plot.status, "GROWING")  # unchanged

    # ── day-close tests ───────────────────────────────────────────────────────

    def test_day_close_success(self):
        """Valid day-close returns 200 with OK."""
        # Set time to day 1 (minute 1440+)
        self.time_state.world_minute = 2000
        self.time_state.save()

        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "phase3:day-close:0"))
        payload = {"day_index": 0, "request_id": request_id}

        resp = self.client.post(
            "/api/village/economy/day-close",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["result_code"], ResultCode.OK)
        self.assertFalse(data["replayed"])

    def test_day_close_idempotent(self):
        """Same request_id returns replayed=true."""
        self.time_state.world_minute = 2000
        self.time_state.save()

        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "phase3:day-close:0"))
        payload = {"day_index": 0, "request_id": request_id}

        # First call
        self.client.post(
            "/api/village/economy/day-close",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )

        # Replay
        resp = self.client.post(
            "/api/village/economy/day-close",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["replayed"])

    def test_day_close_sequential(self):
        """Skip day returns P3_DAY_NOT_READY."""
        self.time_state.world_minute = 5000  # day 3
        self.time_state.save()

        # Try to close day 2 without closing day 0 and 1 first
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "phase3:day-close:2"))
        payload = {"day_index": 2, "request_id": request_id}

        resp = self.client.post(
            "/api/village/economy/day-close",
            payload,
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["result_code"], ResultCode.P3_DAY_NOT_READY)

    def test_day_close_unauthenticated(self):
        """No auth returns 401."""
        resp = self.client.post(
            "/api/village/economy/day-close",
            {"day_index": 0, "request_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_day_close_rejects_unknown_fields(self):
        """Unknown field returns 400."""
        resp = self.client.post(
            "/api/village/economy/day-close",
            {"day_index": 0, "request_id": str(uuid.uuid4()), "bad": True},
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertEqual(data["result_code"], ResultCode.P3_INVALID_PARAMS)

    def test_day_close_rejects_invalid_day_index(self):
        """Invalid day_index returns 400."""
        resp = self.client.post(
            "/api/village/economy/day-close",
            {"day_index": -1, "request_id": str(uuid.uuid4())},
            format="json",
            HTTP_AUTHORIZATION=self._auth_header(),
        )
        self.assertEqual(resp.status_code, 400)

    # ── Phase-six routes are protected and reachable ──────────────────────────

    def test_phase_six_routes_are_not_removed(self):
        """The formerly disabled authority routes now exist behind auth."""
        client = APIClient()
        for path in (
            "/api/village/runtime/time",
            "/api/village/life-actions/execute",
            "/api/village/commands/accept",
            f"/api/village/commands/{uuid.uuid4()}/start",
            "/api/village/runtime/advance-needs",
        ):
            resp = client.post(path, {}, format="json")
            self.assertNotEqual(resp.status_code, 404, f"{path} must be registered")
