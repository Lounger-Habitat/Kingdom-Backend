import uuid

from django.test import TestCase

from village.models import VillageDeliveryOutbox, VillageWorldEvent
from village.phase4_visibility import ENV_CLIENTS, enqueue_event_deliveries


class PhaseFourVisibilityProjectionTest(TestCase):
    def event(self, visibility, participants=None, witnesses=None, players=None, public_data=None):
        return VillageWorldEvent.objects.create(
            dedupe_key=f"visibility-agent-{uuid.uuid4()}",
            event_type="PHASE4_TEST",
            world_minute=100,
            time_revision=1,
            participant_npc_ids=participants or [],
            witness_npc_ids=witnesses or [],
            player_ids=players or [],
            point_code="INTERNAL_POINT",
            command_id=uuid.uuid4(),
            request_id=uuid.uuid4(),
            cause_event_id=uuid.uuid4(),
            visibility=visibility,
            private_data={"secret": "npc-only"},
            public_data=public_data or {
                "safe": True,
                "category": "ECONOMY",
                "public_status_code": "WORKING",
                "needs": {"hunger": 99},
            },
            effects={"money": 99},
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
                    self.assertNotIn("public_data", entry.payload)

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
        self.assertNotIn("public_data", player_entry.payload)
        npc_entry = next(entry for entry in entries if entry.recipient == "10011")
        self.assertEqual(npc_entry.payload["public_data"]["safe"], True)
        self.assertEqual(npc_entry.payload["public_data"]["private_data"], {"secret": "npc-only"})

    def test_env_client_payload_is_exact_public_whitelist(self):
        event = self.event("PLAYER_PRIVATE", players=["player-a"])
        payload = next(
            entry.payload for entry in enqueue_event_deliveries(event)
            if entry.recipient == ENV_CLIENTS
        )
        self.assertEqual(set(payload), {
            "schema_version", "event_id", "event_sequence", "event_type", "world_minute",
            "actor_npc_id", "participant_npc_ids", "location_code", "visibility", "summary",
            "player_ids", "category", "public_status_code",
        })
        self.assertEqual(payload["category"], "ECONOMY")
        self.assertEqual(payload["public_status_code"], "WORKING")
        for forbidden in (
            "time_revision", "point_code", "command_id", "request_id", "cause_event_id",
            "witness_npc_ids", "public_data", "private_data", "hub_summary", "effects", "needs",
        ):
            self.assertNotIn(forbidden, payload)

        unsafe = self.event(
            "PLAYER_PRIVATE",
            players=["player-a"],
            public_data={"category": "not safe", "public_status_code": "X" * 65},
        )
        unsafe_payload = next(
            entry.payload for entry in enqueue_event_deliveries(unsafe)
            if entry.recipient == ENV_CLIENTS
        )
        self.assertNotIn("category", unsafe_payload)
        self.assertNotIn("public_status_code", unsafe_payload)
