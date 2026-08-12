import time
import uuid

from django.test import TestCase, override_settings

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.models import NpcRuntimeState
from village.runtime_services import CommandService, ResultCode


@override_settings(VILLAGE_FAST_COMMAND_INTEGRATION=True)
class FastCommandIntegrationTests(TestCase):
    def setUp(self):
        self.npc = NpcDefinition.objects.create(
            agent_id=10011, name="bill", is_test_only=False,
        )
        self.runtime = NpcRuntimeState.objects.create(
            npc=self.npc,
            current_location_code="VILLAGE_SQUARE",
            action_status="ACTING",
            state_version=7,
            needs_world_minute=0,
        )
        self.time_state = GlobalGameTimeState.get_instance()
        self.time_state.world_minute = 1000
        self.time_state.time_revision = 3
        self.time_state.save()

    def test_accept_and_start_bypass_nonessential_gates(self):
        command_id = uuid.uuid4()
        request_id = uuid.uuid4()
        content = {
            "schema_version": "legacy-value",
            "id": str(command_id),
            "request_id": str(request_id),
            "name": "rest",
            "params": {"ignored_debug_field": True},
            "queue_mode": "RELAXED",
            "expected_state_version": 999,
            "not_before_world_minute": 9000,
            "expires_world_minute": 1,
            "duration_minutes": 31,
            "ttl_ms": -1,
        }

        accepted, code, command = CommandService.accept_command(
            envelope_id=str(uuid.uuid4()),
            sender="10011",
            recipient="wrong-env-is-allowed-in-fast-mode",
            content=content,
            timestamp_ms=int(time.time() * 1000) - 10_000_000,
        )

        self.assertTrue(accepted)
        self.assertEqual(code, ResultCode.COMMAND_ACCEPTED)
        self.assertEqual(command.duration_minutes, 60)
        self.assertEqual(command.validated_at_state_version, 7)
        self.assertIsNone(command.expires_world_minute)

        started, code, command = CommandService.start_command(command_id)
        self.assertTrue(started)
        self.assertEqual(code, ResultCode.COMMAND_ACCEPTED)
        self.runtime.refresh_from_db()
        self.assertEqual(self.runtime.action_status, "ACTING")
        self.assertEqual(self.runtime.current_action, "REST")
        self.assertEqual(self.runtime.needs_world_minute, 1000)

    def test_unknown_action_and_duplicate_identity_are_still_guarded(self):
        content = {
            "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "name": "delete_everything",
            "params": {},
        }
        accepted, code, _ = CommandService.accept_command(
            str(uuid.uuid4()), "10011", "env", content, None,
        )
        self.assertFalse(accepted)
        self.assertEqual(code, ResultCode.UNKNOWN_ACTION)

        envelope_id = str(uuid.uuid4())
        valid = {
            "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "name": "move",
            "params": {"location_code": "VILLAGE_SQUARE"},
        }
        first = CommandService.accept_command(envelope_id, "10011", "env", valid, None)
        replay = CommandService.accept_command(envelope_id, "10011", "env", valid, None)
        self.assertTrue(first[0])
        self.assertTrue(replay[0])
        self.assertEqual(replay[1], ResultCode.COMMAND_REPLAYED)
        self.assertEqual(self.npc.village_commands.count(), 1)
