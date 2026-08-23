import time
import uuid

from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.models import NpcRuntimeState, VillageDeliveryOutbox, VillageWorldEvent
from village.runtime_services import CommandService, OutboxService, ResultCode


class PhaseSixRuntimeTests(TestCase):
    def setUp(self):
        self.npc, _ = NpcDefinition.objects.get_or_create(
            agent_id=10011, defaults={"name": "phase6-npc", "is_test_only": False}
        )
        NpcRuntimeState.objects.update_or_create(
            npc=self.npc,
            defaults={"current_location_code": "VILLAGE_SQUARE", "action_status": "IDLE", "state_version": 0},
        )
        GlobalGameTimeState.objects.update_or_create(pk=1, defaults={"world_minute": 100, "time_revision": 1})

    def _accept(self):
        return CommandService.accept_command(
            str(uuid.uuid4()), "10011", "env-server-001",
            {
                "schema_version": "village.command.v1", "id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "name": "WAIT", "params": {},
                "expected_state_version": 0, "queue_mode": "STRICT", "ttl_ms": 30000,
            }, int(time.time() * 1000),
        )

    def test_cancel_is_durable_and_has_final_outcome(self):
        ok, code, command = self._accept()
        self.assertTrue(ok, code)
        ok, code, _ = CommandService.cancel_command(command.command_id)
        self.assertTrue(ok, code)
        command.refresh_from_db()
        self.assertEqual(command.status, "CANCELLED")
        self.assertTrue(VillageDeliveryOutbox.objects.filter(command=command, message_kind="OUTCOME").exists())

    def test_event_ack_advances_recipient_cursor(self):
        event = VillageWorldEvent.objects.create(
            dedupe_key="phase6:test:event", event_type="TEST", world_minute=100,
            time_revision=1, visibility="PUBLIC", hub_summary="test"
        )
        entry = VillageDeliveryOutbox.objects.create(
            dedupe_key="phase6:test:delivery", recipient="10011", message_kind="EVENT",
            event=event, payload={"schema_version": "village.event.v1", "event_sequence": event.event_sequence},
            priority=1
        )
        claimed = OutboxService.claim_pending("10011")
        self.assertEqual(claimed[0]["delivery_id"], str(entry.delivery_id))
        self.assertEqual(
            OutboxService.ack_delivery(entry.delivery_id, event_sequence=event.event_sequence, recipient="10011")[0],
            True,
        )
        self.assertEqual(OutboxService.get_cursor("10011"), event.event_sequence)
