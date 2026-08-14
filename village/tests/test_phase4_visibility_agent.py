import uuid

from django.test import TestCase

from village.models import VillageDeliveryOutbox, VillageWorldEvent
from village.phase4_visibility import ENV_CLIENTS, enqueue_event_deliveries


class PhaseFourVisibilityProjectionTest(TestCase):
    def event(self, visibility, participants=None, witnesses=None, players=None):
        return VillageWorldEvent.objects.create(
            dedupe_key=f"visibility-agent-{uuid.uuid4()}",
            event_type="PHASE4_TEST",
            world_minute=100,
            time_revision=1,
            participant_npc_ids=participants or [],
            witness_npc_ids=witnesses or [],
            player_ids=players or [],
            visibility=visibility,
            private_data={"secret": "npc-only"},
            public_data={"safe": True},
            hub_summary="hub",
            player_summary="player",
        )

    def test_five_visibility_classes_project_to_existing_outbox(self):
        cases = [
            ("PUBLIC", [], [], [], {"10011", "10012", "10013", "10014", "10015", ENV_CLIENTS}),
            ("PARTICIPANTS", ["10011", "10012"], [], [], {"10011", "10012"}),
            ("PRESENT", ["10011"], ["10013"], [], {"10011", "10013"}),
            ("PLAYER_PRIVATE", [], [], ["player-a"], {ENV_CLIENTS}),
            ("SYSTEM", ["10011"], ["10013"], ["player-a"], set()),
        ]
        for index, (visibility, participants, witnesses, players, expected) in enumerate(cases):
            event = self.event(visibility, participants, witnesses, players)
            entries = enqueue_event_deliveries(event)
            self.assertEqual({entry.recipient for entry in entries}, expected)
            self.assertEqual(len(enqueue_event_deliveries(event)), len(expected))
            for entry in entries:
                if entry.recipient == ENV_CLIENTS:
                    self.assertNotIn("private_data", entry.payload["public_data"])

        self.assertEqual(
            VillageDeliveryOutbox.objects.filter(message_kind="EVENT").count(),
            sum(len(case[-1]) for case in cases),
        )

    def test_old_private_is_participants_and_player_payload_is_summary_only(self):
        event = self.event("PRIVATE", ["10011", "10012"], players=["player-a"])
        entries = enqueue_event_deliveries(event)
        self.assertEqual({entry.recipient for entry in entries}, {"10011", "10012", ENV_CLIENTS})
        player_entry = next(entry for entry in entries if entry.recipient == ENV_CLIENTS)
        self.assertEqual(player_entry.payload["visibility"], "PARTICIPANTS")
        self.assertNotIn("private_data", player_entry.payload["public_data"])
