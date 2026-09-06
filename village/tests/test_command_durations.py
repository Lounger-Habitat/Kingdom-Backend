import time
import uuid

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.command_catalog import COMMAND_CATALOG
from village.models import NpcCommitment, NpcRuntimeState, VillageActionDurationConfig, VillageSimulationConfig, VillageWorldEvent
from village.phase2_services import NeedsService
from village.runtime_services import CommandService, ResultCode
from village.views import commands_accept, commands_start


class CommandDurationTests(TestCase):
    def setUp(self):
        self.npc = NpcDefinition.objects.create(agent_id=10011, name="bill")
        self.runtime = NpcRuntimeState.objects.create(npc=self.npc, current_location_code="BILL_HOME")
        VillageSimulationConfig.objects.update_or_create(
            config_key="default", defaults={"village_needs_enabled": True, "command_queue_limit": 100},
        )
        self.clock = GlobalGameTimeState.get_instance()

    def content(self, action, **values):
        return {
            "schema_version": "village.command.v1", "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()), "name": action, "params": {},
            "queue_mode": "STRICT", "expected_state_version": 0,
            "not_before_world_minute": 0, "ttl_ms": 86400000, **values,
        }

    def accept(self, content, envelope_id=None):
        return CommandService.accept_command(
            envelope_id or str(uuid.uuid4()), "10011", "env", content, int(time.time() * 1000),
        )

    def configure(self, action, duration):
        row, _ = VillageActionDurationConfig.objects.update_or_create(
            action_name=action, defaults={"duration_minutes": duration},
        )
        row.full_clean()

    def test_both_modes_use_defaults_and_preserve_optional_agent_minutes(self):
        for fast in (False, True):
            with self.settings(VILLAGE_FAST_COMMAND_INTEGRATION=fast):
                for action in ("WAIT", "EAT", "PLANT", "CREATE_COMMITMENT", "CANCEL_COMMITMENT"):
                    with self.subTest(fast=fast, action=action):
                        self.configure(action, 7)
                        params = {"source": "COMMUNITY_MEAL"} if action == "EAT" else {}
                        ok, code, command = self.accept(self.content(action, params=params, duration_minutes=30))
                        self.assertTrue(ok, code)
                        self.assertEqual(command.duration_minutes, 7)
                for action in ("REST", "SLEEP", "WORK_PLACEHOLDER"):
                    self.configure(action, 45)
                    for params, expected in (({}, 45), ({"duration_minutes": 75}, 75), ({"duration_minutes": 7}, 7)):
                        with self.subTest(fast=fast, action=action, params=params):
                            ok, code, command = self.accept(self.content(action, params=params))
                            self.assertTrue(ok, code)
                            self.assertEqual(command.duration_minutes, expected)
                            self.assertNotIn("duration_minutes", command.sanitized_parameters)

    def test_invalid_agent_values_are_rejected_without_rounding(self):
        for fast in (False, True):
            with self.settings(VILLAGE_FAST_COMMAND_INTEGRATION=fast):
                for value in (0, -1, True, "75", 7.5, None):
                    for wire in ({"duration_minutes": value}, {"params": {"duration_minutes": value}}):
                        with self.subTest(fast=fast, wire=wire):
                            ok, code, _ = self.accept(self.content("REST", **wire))
                            self.assertFalse(ok)
                            self.assertEqual(code, ResultCode.INVALID_ACTION_DURATION)
                self.assertFalse(self.accept(self.content("PLANT", params={"duration_minutes": 7}))[0])

    def test_retry_and_start_keep_accepted_duration_after_config_change(self):
        for fast in (False, True):
            with self.settings(VILLAGE_FAST_COMMAND_INTEGRATION=fast):
                self.configure("WAIT", 7)
                content, envelope = self.content("WAIT"), str(uuid.uuid4())
                command = self.accept(content, envelope)[2]
                self.configure("WAIT", 45)
                replay = self.accept(content, envelope)
                self.assertEqual((replay[0], replay[1], replay[2].duration_minutes), (True, ResultCode.COMMAND_REPLAYED, 7))
                ok, code, started = CommandService.start_command(command.command_id, 0)
                self.assertTrue(ok, code)
                self.assertEqual(started.duration_minutes, 7)
                self.runtime.refresh_from_db()
                self.assertEqual(self.runtime.action_ends_world_minute, 7)
                self.runtime.action_status = "IDLE"
                self.runtime.active_command_id = None
                self.runtime.save()

    def test_commitment_type_selects_configuration_at_accept(self):
        other, _ = NpcDefinition.objects.get_or_create(agent_id=10012, defaults={"name": "jack"})
        event = VillageWorldEvent.objects.create(dedupe_key="duration-test", event_type="test", world_minute=0, time_revision=0)
        for kind, minutes in (("JOINT_WORK", 47), ("MEETING", 19), ("DONATION", 3)):
            self.configure(kind if kind != "DONATION" else "PERFORM_COMMITMENT", minutes)
            commitment = NpcCommitment.objects.create(
                promisor_npc=self.npc, beneficiary_npc=other, commitment_type=kind,
                target={}, created_world_minute=0, due_world_minute=1440, source_request_id=uuid.uuid4(), created_event=event,
            )
            for fast in (False, True):
                with self.settings(VILLAGE_FAST_COMMAND_INTEGRATION=fast):
                    ok, code, command = self.accept(self.content(
                        "PERFORM_COMMITMENT", params={"commitment_id": str(commitment.commitment_id)},
                    ))
                    self.assertTrue(ok, code)
                    self.assertEqual(command.duration_minutes, minutes)

    def test_accept_and_start_api_return_authoritative_timing(self):
        self.configure("WAIT", 7)
        factory = APIRequestFactory()
        request = factory.post("/", {
            "envelope_id": str(uuid.uuid4()), "sender": "10011", "recipient": "env",
            "content": self.content("WAIT"), "timestamp": int(time.time() * 1000),
        }, format="json")
        force_authenticate(request, user=type("Env", (), {"is_authenticated": True})())
        accepted = commands_accept(request)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.data["data"]["duration_minutes"], 7)
        self.configure("WAIT", 45)
        request = factory.post("/", {"start_world_minute": 0}, format="json")
        force_authenticate(request, user=type("Env", (), {"is_authenticated": True})())
        started = commands_start(request, accepted.data["data"]["command_id"])
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.data["data"]["duration_minutes"], 7)
        self.assertEqual(started.data["data"]["ends_world_minute"], 7)

    def test_optional_schema_and_fractional_needs_survive_short_actions(self):
        for action in CommandService.VARIABLE_DURATION_ACTIONS:
            schema = COMMAND_CATALOG[action]["parameters"]
            self.assertNotIn("duration_minutes", schema["required"])
            self.assertEqual(schema["properties"]["duration_minutes"]["minimum"], 1)
            self.assertNotIn("maximum", schema["properties"]["duration_minutes"])
            self.assertNotIn("multipleOf", schema["properties"]["duration_minutes"])
        whole = NpcRuntimeState(npc=self.npc)
        for _ in range(7):
            NeedsService._apply_scaled_rate(self.runtime, "social", -15, 1, 25)
        NeedsService._apply_scaled_rate(whole, "social", -15, 7, 25)
        self.runtime.save()
        self.runtime.refresh_from_db()
        self.assertEqual((self.runtime.social_fp, self.runtime.social_remainder), (whole.social_fp, whole.social_remainder))

    def test_precision_migration_preserves_existing_fractional_needs(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection

        migration = import_module("village.migrations.0029_needs_remainder_precision")
        self.runtime.hunger_remainder, self.runtime.social_remainder = 20, -15
        self.runtime.save()
        migration.increase_precision(apps, connection.schema_editor())
        self.runtime.refresh_from_db()
        self.assertEqual((self.runtime.hunger_remainder, self.runtime.social_remainder), (2000, -1500))
        migration.restore_precision(apps, connection.schema_editor())
        self.runtime.refresh_from_db()
        self.assertEqual((self.runtime.hunger_remainder, self.runtime.social_remainder), (20, -15))
