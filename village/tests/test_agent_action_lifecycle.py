import time
import uuid
from types import SimpleNamespace

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.action_lifecycle import current_epoch, runtime_operation, runtime_resources
from village.models import NpcCommandState, NpcRuntimeState, VillageActionReceipt, VillageSimulationConfig
from village.runtime_services import CommandService, OutboxService


@override_settings(VILLAGE_FAST_COMMAND_INTEGRATION=True)
class AgentActionLifecycleTests(TestCase):
    def setUp(self):
        self.npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        NpcRuntimeState.objects.get_or_create(npc=self.npc)
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={"village_needs_enabled": True})
        GlobalGameTimeState.get_instance()
        self.execution = {"operation_key": uuid.uuid4().hex, "action_id": "original-star-action",
                          "epoch": current_epoch(), "request": {"name": "REST", "params": {"duration_minutes": 40}}}

    def accept(self, execution=None):
        return CommandService.accept_command(str(uuid.uuid4()), "10011", "env", {
            "schema_version": "village.command.v1", "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()),
            "name": "REST", "params": {}, "duration_minutes": 40, "queue_mode": "STRICT",
            "ttl_ms": 86400000, "agent_execution": execution or self.execution}, int(time.time() * 1000))

    def query(self, view, npc_id="10011", **params):
        request = APIRequestFactory().get("/", params)
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        return view(request, npc_id).data["data"]

    def test_accept_replay_and_conflict_do_not_enqueue_more_work(self):
        ok, code, command = self.accept()
        self.assertTrue(ok, code)
        replay = self.accept()
        self.assertEqual(replay[2].pk, command.pk)
        other = {**self.execution, "operation_key": uuid.uuid4().hex}
        self.assertEqual(self.accept(other)[:2], (False, "NPC_BUSY"))
        changed = {**self.execution, "request": {"name": "REST", "params": {"duration_minutes": 5}}}
        self.assertEqual(self.accept(changed)[:2], (False, "REQUEST_ID_CONFLICT"))
        self.assertEqual(NpcCommandState.objects.filter(npc=self.npc).count(), 1)

    def test_pending_operation_is_queryable_and_keeps_body_claim(self):
        self.assertTrue(self.accept()[0])
        result = self.query(runtime_operation, operation_key=self.execution["operation_key"])
        self.assertEqual((result["status"], result["request"]), ("accepted", self.execution["request"]))
        resources = self.query(runtime_resources)
        self.assertEqual(resources["epoch"], self.execution["epoch"])
        self.assertEqual(resources["claims"][0]["resources"], ["body"])
        self.assertEqual(self.query(runtime_operation, "10012", operation_key=self.execution["operation_key"])["status"], "not_found")

    def test_terminal_outbox_and_recovery_keep_original_identity(self):
        command = self.accept()[2]
        command.status = "FAILED"
        command.save()
        receipt = VillageActionReceipt.objects.create(request_id=command.request_id, command=command, npc=self.npc,
            action_kind="REST", success=False, result_code="COMMAND_CANCELLED_ON_RECOVERY",
            world_minute=100, time_revision=1, response_data={"data": {"actual_duration_minutes": 5}})
        OutboxService.ensure_outcome(receipt)
        result = self.query(runtime_operation, operation_key=self.execution["operation_key"])
        delivery = OutboxService.claim_pending("10011")[0]["envelope"]["payload"]["content"]
        self.assertEqual(result["status"], "terminal")
        self.assertFalse(result["outcome"]["success"])
        self.assertEqual(delivery["agent_execution"], self.execution)
        self.assertEqual(result["outcome"]["agent_execution"], self.execution)
        self.assertEqual(self.query(runtime_resources)["claims"], [])
