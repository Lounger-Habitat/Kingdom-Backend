import uuid
from types import SimpleNamespace

from django.test import TestCase

from inventory.models import NpcDefinition
from village.models import VillageDeliveryOutbox, VillageWorldEvent
from village.phase4_visibility import ENV_CLIENTS, _recipients, enqueue_event_deliveries


class PhaseFourVisibilityProjectionTest(TestCase):
    def event(self, visibility, participants=None, witnesses=None, players=None, public_data=None, *, event_type="PHASE4_TEST", effects=None):
        return VillageWorldEvent.objects.create(
            dedupe_key=f"visibility-agent-{uuid.uuid4()}",
            event_type=event_type,
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
            effects=effects or {"money": 99},
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

    def test_economy_food_event_recipients_and_action_event_suppression(self):
        from village import phase4_visibility

        eat = self.event("PUBLIC", ["10011"], ["10014"], event_type="ECONOMY_EAT")
        # 默认（二跑复盘 R1）：动作类事件只存档不投递。
        self.assertEqual(set(_recipients(eat)), set())
        self.assertEqual(enqueue_event_deliveries(eat), [])
        self.assertEqual(VillageWorldEvent.objects.filter(event_id=eat.event_id).count(), 1)

        # 恢复开关后回到 actor+participants+witnesses+ENV_CLIENTS 的受众规则。
        phase4_visibility.SUPPRESS_ACTION_EVENT_DELIVERY = False
        try:
            self.assertEqual(
                {entry.recipient for entry in enqueue_event_deliveries(eat)},
                {"10011", "10014", ENV_CLIENTS},
            )
        finally:
            phase4_visibility.SUPPRESS_ACTION_EVENT_DELIVERY = True

    def test_trade_action_event_only_notifies_counterparty(self):
        actor, _ = NpcDefinition.objects.get_or_create(
            agent_id="10011", defaults={"name": "actor"}
        )
        event = self.event(
            "PARTICIPANTS", ["10011", "10012"], event_type="ECONOMY_PROPOSE_TRADE"
        )
        event.actor_npc = actor
        self.assertEqual(set(_recipients(event)), {"10012"})

        event = self.event(
            "PARTICIPANTS", ["10011", "10012"], event_type="ECONOMY_RESPOND_TRADE"
        )
        actor, _ = NpcDefinition.objects.get_or_create(
            agent_id="10012", defaults={"name": "counterparty"}
        )
        event.actor_npc = actor
        self.assertEqual(set(_recipients(event)), {"10011"})

    def test_terminal_action_events_suppressed_while_story_events_still_broadcast(self):
        completed = self.event("PUBLIC", ["10011"], [], event_type="NPC_ACTION_COMPLETED")
        failed = self.event("PUBLIC", ["10011"], [], event_type="NPC_ACTION_FAILED")
        interrupted = self.event("PUBLIC", ["10011"], [], event_type="npc_action_interrupted")
        self.assertEqual(_recipients(completed), [])
        self.assertEqual(_recipients(failed), [])
        self.assertEqual(_recipients(interrupted), [])

        # 非动作事件（剧情/日结/系统）不受影响，仍按 PUBLIC 全播。
        story = self.event("PUBLIC", ["10011"], [], event_type="STORY_PHASE_ADVANCED")
        self.assertEqual(
            {entry.recipient for entry in enqueue_event_deliveries(story)},
            {"10011", "10012", "10013", "10014", "10015", ENV_CLIENTS},
        )

    def test_audit_unknown_broadcasts_and_food_always_includes_actor(self):
        from village import phase4_visibility

        actor = SimpleNamespace(agent_id="10013")
        # 开关恢复时保留原有受众规则断言（未来恢复广播的回归保障）。
        phase4_visibility.SUPPRESS_ACTION_EVENT_DELIVERY = False
        try:
            for effects in ({"audit": {"balance_matches": True}}, {"audit": {"balance_matches": False}}, {"audit": []}):
                event = SimpleNamespace(
                    visibility="PUBLIC", event_type="ECONOMY_AUDIT_PUBLIC_FUND",
                    participant_npc_ids=[], witness_npc_ids=[], player_ids=[], actor_npc=actor,
                    effects=effects,
                )
                recipients = set(_recipients(event))
                expected = {"10013", ENV_CLIENTS} if effects["audit"] == {"balance_matches": True} else {
                    "10011", "10012", "10013", "10014", "10015", ENV_CLIENTS,
                }
                self.assertEqual(recipients, expected)

            event = SimpleNamespace(
                visibility="PUBLIC", event_type="ECONOMY_HUNT",
                participant_npc_ids=[], witness_npc_ids=["10014"], player_ids=[], actor_npc=actor,
                effects={},
            )
            self.assertEqual(set(_recipients(event)), {"10013", "10014", ENV_CLIENTS})
        finally:
            phase4_visibility.SUPPRESS_ACTION_EVENT_DELIVERY = True
        # 默认开关开启：动作类事件零投递。
        event = SimpleNamespace(
            visibility="PUBLIC", event_type="ECONOMY_HUNT",
            participant_npc_ids=[], witness_npc_ids=["10014"], player_ids=[], actor_npc=actor,
            effects={},
        )
        self.assertEqual(_recipients(event), [])

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
