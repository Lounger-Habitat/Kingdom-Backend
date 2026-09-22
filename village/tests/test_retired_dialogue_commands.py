import json
import time
import uuid
from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.command_catalog import COMMAND_CATALOG
from village.models import (
    NpcCommandState, NpcProfile, NpcRuntimeState, NpcDialogueSession, NpcSocialDailyCounter,
    VillageActionDurationConfig, VillageActionReceipt, VillageLocationDefinition,
    VillageDeliveryOutbox, VillageSimulationConfig, VillageWorldEvent,
)
from village.runtime_services import CommandService, ResultCode
from village.seed_data import BASE_ACTIONS


class RetiredDialogueCommandTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(
            config_key="default", defaults={"village_needs_enabled": True},
        )
        VillageLocationDefinition.objects.update_or_create(
            location_code="SQUARE", defaults={"display_name": "Square", "center_anchor_code": "SQUARE",
                                               "location_type": "public", "is_public": True},
        )
        for npc_id in ("10011", "10012", "10013", "10014", "10015"):
            npc, _ = NpcDefinition.objects.update_or_create(
                agent_id=npc_id, defaults={"name": npc_id, "is_test_only": False},
            )
            NpcProfile.objects.update_or_create(npc=npc, defaults={
                "display_name": npc_id, "occupation": "test", "is_active": True,
                "home_location_code": "SQUARE", "work_location_code": "SQUARE",
                "social_location_code": "SQUARE", "capabilities": ["MOVE", "GO_HOME", "GET_RULE"],
            })
            NpcRuntimeState.objects.update_or_create(npc=npc, defaults={
                "current_location_code": "SQUARE", "action_status": "IDLE", "needs_world_minute": 0,
            })
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 0
        self.clock.time_revision = 1
        self.clock.save(update_fields=["world_minute", "time_revision"])
        self.client = APIClient()
        self.client.force_authenticate(GameAccount.objects.create(username="speech-test-env", password="unused"))

    def test_all_npcs_discover_and_rules_exclude_retired_tools_even_with_stale_permissions(self):
        self.assertTrue({"START_DIALOGUE", "SPEAK", "SPEECH"}.isdisjoint(COMMAND_CATALOG))
        self.assertTrue({"START_DIALOGUE", "SPEAK"}.isdisjoint(BASE_ACTIONS))
        for profile in NpcProfile.objects.all():
            profile.capabilities += ["START_DIALOGUE", "SPEAK"]
            profile.save(update_fields=["capabilities"])
            npc_id = str(profile.npc.agent_id)
            for suffix in ("discover", "available-actions"):
                response = self.client.get(f"/api/village/npcs/{npc_id}/{suffix}")
                self.assertEqual(response.status_code, 200, response.data)
                text = json.dumps(response.data)
                self.assertNotIn("START_DIALOGUE", text)
                self.assertNotIn('"SPEAK"', text)
                self.assertIn("speech", text)
            response = self.client.get(f"/api/village/npcs/{npc_id}/query/GET_RULE")
            self.assertEqual(response.status_code, 200, response.data)
            text = json.dumps(response.data)
            self.assertIn("payload.content.name=speech", text)
            self.assertNotIn("START_DIALOGUE", text)
            self.assertNotIn("SPEAK", text)

    def test_retired_actions_are_rejected_in_both_command_modes(self):
        profile = NpcProfile.objects.get(npc__agent_id="10013")
        profile.capabilities += ["START_DIALOGUE", "SPEAK"]
        profile.save(update_fields=["capabilities"])
        for fast in (False, True):
            for action in ("START_DIALOGUE", "SPEAK", "SPEECH"):
                with self.subTest(fast=fast, action=action), patch(
                    "village.runtime_services._fast_integration_enabled", return_value=fast,
                ):
                    result = CommandService.accept_command(str(uuid.uuid4()), "10013", "env", {
                        "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()),
                        "schema_version": "village.command.v1", "name": action, "params": {},
                        "queue_mode": "STRICT", "ttl_ms": 120000,
                    }, int(time.time() * 1000))
                    self.assertEqual(result, (False, ResultCode.UNKNOWN_ACTION, None))

    def test_migration_only_removes_retired_permissions_and_is_idempotent(self):
        for profile in NpcProfile.objects.all():
            profile.capabilities = ["MOVE", "START_DIALOGUE", "GET_RULE", " speak ", "GO_HOME"]
            profile.save(update_fields=["capabilities"])
        before = list(NpcRuntimeState.objects.order_by("pk").values())
        migrate = import_module("village.migrations.0040_retire_dialogue_commands").remove_dialogue_commands
        migrate(apps, connection.schema_editor())
        migrate(apps, connection.schema_editor())
        for profile in NpcProfile.objects.all():
            self.assertEqual(profile.capabilities, ["MOVE", "GET_RULE", "GO_HOME"])
        self.assertEqual(before, list(NpcRuntimeState.objects.order_by("pk").values()))

    def test_wait_retirement_migration_removes_permissions_and_duration(self):
        profile = NpcProfile.objects.order_by("pk").first()
        profile.capabilities = ["MOVE", "WAIT", "REST", " wait "]
        profile.save(update_fields=["capabilities"])
        VillageActionDurationConfig.objects.create(action_name="WAIT", duration_minutes=7)
        runtime = NpcRuntimeState.objects.get(npc=profile.npc)
        command = NpcCommandState.objects.create(
            command_id=uuid.uuid4(), message_id=uuid.uuid4(), request_id=uuid.uuid4(),
            npc=profile.npc, sender_id=str(profile.npc.agent_id), recipient_id="env",
            kind="WAIT", sanitized_parameters={}, payload_sha256="e" * 64,
            status="RUNNING", received_world_minute=0, started_world_minute=0,
        )
        runtime.current_action = "WAIT"
        runtime.current_goal = "WAIT"
        runtime.action_status = "ACTING"
        runtime.active_command_id = command.command_id
        runtime.save()
        migrate = import_module("village.migrations.0063_retire_wait_command").retire_wait_command

        migrate(apps, connection.schema_editor())
        migrate(apps, connection.schema_editor())

        profile.refresh_from_db()
        runtime.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(profile.capabilities, ["MOVE", "REST"])
        self.assertFalse(VillageActionDurationConfig.objects.filter(action_name="WAIT").exists())
        self.assertEqual(
            (runtime.current_action, runtime.current_goal, runtime.action_status, runtime.active_command_id),
            ("", "NONE", "IDLE", None),
        )
        self.assertEqual((command.status, command.result_code), ("FAILED", "ACTION_RETIRED"))
        self.assertTrue(VillageActionReceipt.objects.filter(command=command).exists())
        self.assertTrue(VillageDeliveryOutbox.objects.filter(command=command).exists())

    def test_speech_session_http_chain_still_settles_once_without_old_permissions(self):
        conversation = "event-speech-session"
        start = self.client.post("/api/village/dialogues/start", {
            "npc_id": "10013", "target_npc_id": "10011", "conversation_id": conversation,
        }, format="json")
        self.assertEqual(start.status_code, 200, start.data)
        for npc_id, text in (("10013", "来讨论修桥"), ("10011", "好，我说说农田的情况")):
            response = self.client.post(f"/api/village/dialogues/{conversation}/speak", {
                "npc_id": npc_id, "text": text, "end_turn": False,
            }, format="json")
            self.assertEqual(response.status_code, 200, response.data)
        self.clock.world_minute = 30
        self.clock.save(update_fields=["world_minute"])
        for _ in range(2):
            response = self.client.post(f"/api/village/dialogues/{conversation}/complete", {
                "npc_id": "10013", "reason": "COMPLETED",
            }, format="json")
            self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(NpcDialogueSession.objects.get(pk=conversation).valid_for_social_needs)
        self.assertEqual(NpcSocialDailyCounter.objects.get().socialize_count, 1)
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="DIALOGUE_SETTLED").count(), 1)

    def test_speech_receipt_is_recordable_but_retired_command_receipts_are_not(self):
        body = {"npc_id": "10013", "request_id": "event-speech-1", "action": "SPEECH",
                "success": True, "result_code": "DIALOGUE_SPOKEN", "world_minute": 0, "time_revision": 1}
        for expected in ("RECORDED", "REPLAYED"):
            response = self.client.post("/api/village/runtime/instruction-outcome", body, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["resultCode"], expected)
        self.assertEqual(VillageActionReceipt.objects.filter(action_kind="SPEECH").count(), 1)
        for action in ("START_DIALOGUE", "SPEAK"):
            response = self.client.post("/api/village/runtime/instruction-outcome", {
                **body, "action": action, "request_id": str(uuid.uuid4()),
            }, format="json")
            self.assertEqual(response.status_code, 400)
