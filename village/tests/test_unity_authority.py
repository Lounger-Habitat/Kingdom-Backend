import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import NpcDefinition
from village.models import (
    NpcCommandState,
    NpcRuntimeState,
    VillageActionReceipt,
    VillageDeliveryOutbox,
)
from village.trusted_persistence import save_result, save_snapshots


class UnityAuthorityPersistenceTests(TestCase):
    def setUp(self):
        self.npc = NpcDefinition.objects.create(
            agent_id="10011", name="bill", is_test_only=False
        )
        self.runtime = NpcRuntimeState.objects.create(
            npc=self.npc,
            current_location_code="OLD_LOCATION",
            current_point_code="OLD_LOCATION",
            state_version=99,
        )

    def test_snapshot_is_last_write_wins_without_cas(self):
        result = save_snapshots({
            "world_minute": 20_000,
            "time_revision": 7,
            "checkpoints": [{
                "npc_id": "10011",
                "current_location_code": "RESTAURANT",
                "current_point_code": "RESTAURANT",
                "state_version": 3,
                "public_needs": {"hunger": 12.5, "energy": 77.1},
            }],
        })

        self.assertTrue(result["success"])
        self.runtime.refresh_from_db()
        self.assertEqual(self.runtime.current_location_code, "RESTAURANT")
        self.assertEqual(self.runtime.state_version, 3)
        self.assertEqual(self.runtime.hunger_fp, 125)
        self.assertEqual(self.runtime.energy_fp, 771)

    def test_terminal_result_is_saved_without_backend_queue_or_outbox(self):
        command_id = uuid.uuid4()
        request_id = uuid.uuid4()
        result = save_result({
            "command_id": str(command_id),
            "request_id": str(request_id),
            "npc_id": "10011",
            "environment_id": "WuxiVillage",
            "action": "MOVE",
            "success": True,
            "result_code": "MOVE_COMPLETED",
            "world_minute": 20_030,
            "time_revision": 8,
            "state": {
                "current_location_code": "RESTAURANT",
                "current_point_code": "RESTAURANT",
                "current_goal": "NONE",
                "current_action": "",
                "action_status": "IDLE",
                "active_command_id": None,
                "state_version": 4,
                "last_simulated_world_minute": 20_030,
                "needs_world_minute": 20_030,
                "time_revision": 8,
                "public_needs": {"hunger": 14, "energy": 76, "health": 100,
                                 "social": 20, "stress": 10},
            },
        })

        self.assertTrue(result["success"])
        command = NpcCommandState.objects.get(command_id=command_id)
        self.assertEqual(command.status, "SUCCEEDED")
        self.assertEqual(command.queue_mode, "UNITY_LOCAL")
        self.assertIsNone(command.started_world_minute)
        self.assertTrue(VillageActionReceipt.objects.filter(request_id=request_id).exists())
        self.assertFalse(VillageDeliveryOutbox.objects.exists())

    def test_legacy_runtime_authority_routes_are_gone(self):
        client = APIClient()
        for path in (
            "/api/village/runtime/time",
            "/api/village/runtime/advance-needs",
            "/api/village/life-actions/execute",
            "/api/village/commands/accept",
            f"/api/village/commands/{uuid.uuid4()}/start",
        ):
            self.assertEqual(client.post(path, {}, format="json").status_code, 404)

    def test_economy_routes_are_reachable(self):
        """Phase 3 economy routes exist (may return 400/401 but not 404)."""
        client = APIClient()
        for path in (
            "/api/village/economy/actions/complete",
            "/api/village/economy/snapshot",
            "/api/village/economy/day-close",
        ):
            resp = client.post(path, {}, format="json")
            self.assertNotEqual(resp.status_code, 404, f"{path} should not be 404")
