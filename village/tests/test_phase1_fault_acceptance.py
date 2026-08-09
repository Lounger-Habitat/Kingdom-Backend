"""Phase-one fault/recovery acceptance tests through the production services.

These tests deliberately model process boundaries by committing state and then
calling the public service again; no in-memory transport stubs stand in for the
Backend's command, authority-time or Outbox persistence.
"""

import time
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.models import (
    NpcCommandState,
    NpcRuntimeState,
    VillageActionReceipt,
    VillageDeliveryOutbox,
    VillageWorldEvent,
)
from village.runtime_services import (
    ActionService,
    BootstrapService,
    CheckpointService,
    CommandService,
    OutboxService,
    ReservationService,
    ResultCode,
    TimeService,
)


FORMAL_NPC_IDS = ("10011", "10012", "10013", "10014", "10015")


class PhaseOneFaultAcceptanceTests(TestCase):
    def setUp(self):
        self.service_account = GameAccount.objects.create(
            username=f"phase1-env-{uuid.uuid4().hex[:12]}", password="unused"
        )
        self.npcs = {}
        self.runtimes = {}
        for npc_id in FORMAL_NPC_IDS:
            npc, _ = NpcDefinition.objects.get_or_create(
                agent_id=int(npc_id),
                defaults={"name": f"npc-{npc_id}", "is_test_only": False},
            )
            self.assertFalse(npc.is_test_only)
            self.npcs[npc_id] = npc
            runtime, _ = NpcRuntimeState.objects.get_or_create(
                npc=npc,
                defaults={
                    "current_location_code": "VILLAGE_SQUARE",
                    "action_status": "IDLE",
                },
            )
            runtime.current_location_code = "VILLAGE_SQUARE"
            runtime.current_point_code = "VILLAGE_SQUARE"
            runtime.current_goal = "NONE"
            runtime.current_action = ""
            runtime.action_status = "IDLE"
            runtime.action_started_world_minute = None
            runtime.action_ends_world_minute = None
            runtime.active_command_id = None
            runtime.state_version = 0
            runtime.time_revision = 0
            runtime.save()
            self.runtimes[npc_id] = runtime
        self.time_state = GlobalGameTimeState.get_instance()
        self.time_state.world_minute = 10_000
        self.time_state.time_revision = 0
        self.time_state.save()

    def _access_token(self, *, service):
        token = AccessToken.for_user(self.service_account)
        if service:
            token["scope"] = "env_server"
            token["aud"] = "kingdom-env"
        return str(token)

    def _content(self, npc_id, state_version, **overrides):
        content = {
            "schema_version": "village.command.v1",
            "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "name": "move",
            "params": {"location_code": "IRONSMITH"},
            "expected_state_version": state_version,
            "queue_mode": "STRICT",
            "ttl_ms": 30_000,
        }
        content.update(overrides)
        return content

    def _accept(self, npc_id="10011", *, state_version=None, **overrides):
        if state_version is None:
            self.runtimes[npc_id].refresh_from_db()
            state_version = self.runtimes[npc_id].state_version
        content = self._content(npc_id, state_version, **overrides)
        ok, code, command = CommandService.accept_command(
            envelope_id=str(uuid.uuid4()),
            sender=npc_id,
            recipient="env-server-001",
            content=content,
            timestamp_ms=int(time.time() * 1000),
        )
        self.assertTrue(ok, code)
        return content, command

    def _start_and_complete(self, npc_id="10011"):
        _content, command = self._accept(npc_id)
        ok, code, _ = CommandService.start_command(command.command_id)
        self.assertTrue(ok, code)
        ok, code, receipt = ActionService.complete_action(
            command.command_id,
            uuid.uuid4(),
            True,
            ResultCode.MOVE_COMPLETED,
        )
        self.assertTrue(ok, code)
        return command, receipt

    def test_world_minute_crosses_day_and_month_and_survives_backend_restart(self):
        start = GlobalGameTimeState.world_minute_from_calendar(2027, 1, 31, 23, 58)
        self.time_state.world_minute = start
        self.time_state.time_revision = 40
        self.time_state.save()

        for offset in (1, 2):
            ok, code, _receipt = TimeService.advance_time(
                start + offset, 40 + offset - 1, uuid.uuid4()
            )
            self.assertTrue(ok, code)

        # Re-fetching singleton models a Backend process restart: the persisted
        # authority must remain monotonic, including its calendar projection.
        restarted_state = GlobalGameTimeState.get_instance()
        self.assertEqual(restarted_state.world_minute, start + 2)
        self.assertEqual(restarted_state.time_revision, 42)
        self.assertEqual(
            (restarted_state.game_year, restarted_state.game_month,
             restarted_state.game_day, restarted_state.game_hour,
             restarted_state.game_minute),
            (2027, 2, 1, 0, 0),
        )

    def test_checkpoint_cannot_override_backend_authority_fields(self):
        runtime = self.runtimes["10011"]
        runtime.state_version = 7
        runtime.time_revision = 19
        runtime.save()

        result = CheckpointService.submit_checkpoint([{
            "npc_id": "10011",
            "expected_state_version": 7,
            "current_location_code": "TAVERN",
            "state_version": 999,
            "time_revision": 999,
            "world_minute": 999_999,
        }])[0]
        self.assertFalse(result["success"])
        self.assertEqual(result["result_code"], ResultCode.CHECKPOINT_FIELD_NOT_ALLOWED)
        runtime.refresh_from_db()
        self.assertEqual(runtime.current_location_code, "VILLAGE_SQUARE")
        self.assertEqual((runtime.state_version, runtime.time_revision), (7, 19))

    def test_backend_time_unavailable_fails_closed_without_creating_a_receipt(self):
        GlobalGameTimeState.objects.all().delete()
        ok, code, receipt = TimeService.advance_time(10_001, 0, uuid.uuid4())
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.BACKEND_UNAVAILABLE)
        self.assertIsNone(receipt)
        self.assertFalse(VillageActionReceipt.objects.filter(action_kind="TIME_ADVANCE").exists())

    def test_only_env_server_token_can_mutate_shared_runtime_state(self):
        client = APIClient()
        payload = {
            "checkpoints": [{
                "npc_id": "10011",
                "expected_state_version": 0,
                "current_location_code": "TAVERN",
            }]
        }
        self.assertEqual(client.put("/api/village/runtime/checkpoint", payload, format="json").status_code, 401)

        client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._access_token(service=False)}")
        self.assertEqual(client.put("/api/village/runtime/checkpoint", payload, format="json").status_code, 401)

        client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._access_token(service=True)}")
        response = client.put("/api/village/runtime/checkpoint", payload, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["success"])
        self.runtimes["10011"].refresh_from_db()
        self.assertEqual(self.runtimes["10011"].current_location_code, "TAVERN")
        self.assertEqual(self.runtimes["10011"].state_version, 1)

        # The HTTP endpoint must expose a stable batch rejection code when a
        # caller retries a checkpoint with its now-stale CAS version.
        stale_response = client.put("/api/village/runtime/checkpoint", payload, format="json")
        self.assertEqual(stale_response.status_code, 409)
        self.assertFalse(stale_response.data["success"])
        self.assertEqual(stale_response.data["resultCode"], ResultCode.STALE_STATE_VERSION)

    def test_strict_command_is_rechecked_at_start_after_another_action_completes(self):
        first_content, first = self._accept("10011", state_version=0)
        _second_content, second = self._accept("10011", state_version=0)

        ok, code, _ = CommandService.start_command(first.command_id)
        self.assertTrue(ok, code)
        ok, code, _ = ActionService.complete_action(
            first.command_id, uuid.uuid4(), True, ResultCode.MOVE_COMPLETED
        )
        self.assertTrue(ok, code)

        ok, code, command = CommandService.start_command(second.command_id)
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.STALE_STATE_VERSION)
        self.assertIsNone(command)
        second.refresh_from_db()
        self.assertEqual(second.status, "FAILED")
        self.assertTrue(VillageActionReceipt.objects.filter(command=second).exists())

        # The old wire image remains available for Bootstrap/recovery; Env must
        # re-bootstrap rather than overwrite authority with a local checkpoint.
        self.assertEqual(first_content["expected_state_version"], 0)

    def test_duplicate_command_outcome_and_event_do_not_create_more_facts(self):
        content, command = self._accept("10011")
        ok, code, replay = CommandService.accept_command(
            envelope_id=str(command.message_id),
            sender="10011",
            recipient="env-server-001",
            content=content,
            timestamp_ms=int(time.time() * 1000),
        )
        self.assertTrue(ok)
        self.assertEqual(code, ResultCode.COMMAND_REPLAYED)
        self.assertEqual(replay.command_id, command.command_id)

        ok, code, _ = CommandService.start_command(command.command_id)
        self.assertTrue(ok, code)
        request_id = uuid.uuid4()
        ok, code, receipt = ActionService.complete_action(
            command.command_id, request_id, True, ResultCode.MOVE_COMPLETED
        )
        self.assertTrue(ok, code)
        ok, code, replay_receipt = ActionService.complete_action(
            command.command_id, request_id, True, ResultCode.MOVE_COMPLETED
        )
        self.assertTrue(ok)
        self.assertEqual(code, ResultCode.COMMAND_REPLAYED)
        self.assertEqual(replay_receipt.request_id, receipt.request_id)

        # A corrupted redelivery with a fresh request id must not create a new
        # Outcome/Event/Outbox pair after the command is terminal.
        ok, code, _ = ActionService.complete_action(
            command.command_id, uuid.uuid4(), True, ResultCode.MOVE_COMPLETED
        )
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.NPC_BUSY)
        self.assertEqual(VillageActionReceipt.objects.filter(command=command).count(), 1)
        self.assertEqual(VillageWorldEvent.objects.filter(command_id=command.command_id).count(), 1)
        self.assertEqual(VillageDeliveryOutbox.objects.filter(command=command).count(), 2)

    def test_outbox_recovers_after_crash_before_send_and_after_send_before_ack(self):
        command, _receipt = self._start_and_complete("10011")
        entries = OutboxService.claim_pending("10011", limit=10)
        # The command sender is the single intended recipient and each result is
        # only materialized in the durable Outbox, never sent inline by complete.
        self.assertEqual({entry["message_kind"] for entry in entries}, {"OUTCOME", "EVENT"})
        self.assertEqual(
            {entry["delivery_id"] for entry in entries},
            {entry["envelope"]["id"] for entry in entries},
        )

        # Fault injection: process dies after Hub send but before ACK.  On a
        # restarted relay the expired lease is redelivered with the same id.
        VillageDeliveryOutbox.objects.filter(command=command).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        redelivered = OutboxService.claim_pending("10011", limit=10)
        self.assertEqual(
            {entry["delivery_id"] for entry in redelivered},
            {entry["delivery_id"] for entry in entries},
        )
        for entry in redelivered:
            ok, code = OutboxService.ack_delivery(uuid.UUID(entry["delivery_id"]))
            self.assertTrue(ok, code)
        self.assertEqual(
            VillageDeliveryOutbox.objects.filter(command=command, status="ACKED").count(), 2
        )

    def test_complete_persists_outbox_before_any_relay_claim(self):
        command, receipt = self._start_and_complete("10012")
        # Fault injection: Backend exits directly after the transaction commits.
        # A fresh service call sees both final messages without relying on RAM.
        self.assertEqual(VillageDeliveryOutbox.objects.filter(receipt=receipt).count(), 2)
        delivered = OutboxService.claim_pending("10012", limit=10)
        self.assertEqual(len(delivered), 2)
        self.assertTrue(all(item["envelope"]["id"] == item["delivery_id"] for item in delivered))

    def test_reservation_orders_multi_npc_participants_and_recovers_expired_lock(self):
        ok, code, reservation = ReservationService.acquire(
            action_kind="MOVE",
            participant_ids=["10015", "10011", "10012"],
            world_minute=100,
            expires_world_minute=101,
            resource_key="wuxi-bridge",
        )
        self.assertTrue(ok, code)
        self.assertEqual(
            sorted(str(npc.agent_id) for npc in reservation.participants.all()),
            ["10011", "10012", "10015"],
        )

        # Reversed participant input follows the same canonical lock set and
        # conflicts deterministically instead of attempting a lock cycle.
        ok, code, _ = ReservationService.acquire(
            action_kind="MOVE",
            participant_ids=["10012", "10011"],
            world_minute=100,
            expires_world_minute=101,
            resource_key="wuxi-bridge",
        )
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.RESERVATION_CONFLICT)

        ok, code, replacement = ReservationService.acquire(
            action_kind="MOVE",
            participant_ids=["10012", "10011"],
            world_minute=102,
            expires_world_minute=103,
            resource_key="wuxi-bridge",
        )
        self.assertTrue(ok, code)
        self.assertNotEqual(replacement.reservation_id, reservation.reservation_id)

    def test_bootstrap_restores_queued_and_running_command_wire_data(self):
        queued_content, queued = self._accept("10011")
        running_content, running = self._accept("10012")
        ok, code, _ = CommandService.start_command(running.command_id)
        self.assertTrue(ok, code)

        data = BootstrapService.get_bootstrap()
        self.assertEqual([npc["npc_id"] for npc in data["npcs"]], list(FORMAL_NPC_IDS))
        queued_wire = next(item for item in data["commands"] if item["content"]["id"] == str(queued.command_id))
        running_npc = next(item for item in data["npcs"] if item["npc_id"] == "10012")
        self.assertEqual(queued_wire["content"], {
            **queued_content,
            "name": "move",
            "target_id": "",
            "not_before_world_minute": None,
            "expires_world_minute": None,
            "duration_minutes": None,
        })
        self.assertEqual(running_npc["active_command"]["content"]["id"], str(running.command_id))
        self.assertEqual(running_npc["active_command"]["content"]["params"], running_content["params"])

    def test_not_before_command_remains_durable_for_bootstrap_recovery(self):
        content, command = self._accept(
            "10011", not_before_world_minute=self.time_state.world_minute + 5
        )
        ok, code, started = CommandService.start_command(command.command_id)
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.ACTION_NOT_ALLOWED)
        self.assertIsNone(started)
        command.refresh_from_db()
        self.assertEqual(command.status, "QUEUED")
        wire = next(item for item in BootstrapService.get_bootstrap()["commands"]
                    if item["content"]["id"] == str(command.command_id))
        self.assertEqual(wire["content"]["not_before_world_minute"], content["not_before_world_minute"])

    def test_prestart_expiry_is_terminalized_by_backend_and_outbox_recovers(self):
        """An accepted boundary-expiry STRICT command must never need a Unity Outcome.

        The command is valid at accept when ``expires_world_minute == now``.  A
        subsequent authoritative tick makes it expired before StartCommand; Backend
        then owns the terminal receipt/event/outbox transaction and an Outbox relay
        restart reuses the same delivery identities.
        """
        expires_at_accept = self.time_state.world_minute
        _content, command = self._accept(
            "10011", expires_world_minute=expires_at_accept
        )

        self.time_state.refresh_from_db()
        ok, code, _ = TimeService.advance_time(
            self.time_state.world_minute + 1,
            self.time_state.time_revision,
            uuid.uuid4(),
        )
        self.assertTrue(ok, code)

        ok, code, started = CommandService.start_command(command.command_id)
        self.assertFalse(ok)
        self.assertEqual(code, ResultCode.MESSAGE_EXPIRED)
        self.assertIsNone(started)

        command.refresh_from_db()
        self.assertEqual(command.status, "FAILED")
        self.assertEqual(command.result_code, ResultCode.MESSAGE_EXPIRED)
        receipt = VillageActionReceipt.objects.get(command=command)
        self.assertFalse(receipt.success)
        self.assertEqual(receipt.result_code, ResultCode.MESSAGE_EXPIRED)
        event = VillageWorldEvent.objects.get(command_id=command.command_id)
        self.assertEqual(event.event_type, "NPC_ACTION_FAILED")

        initial = OutboxService.claim_pending("10011", limit=10)
        self.assertEqual({item["message_kind"] for item in initial}, {"OUTCOME", "EVENT"})
        self.assertEqual(
            {item["delivery_id"] for item in initial},
            {item["envelope"]["id"] for item in initial},
        )

        # Fault injection: relay crashed after send and before ACK. The recovered
        # relay must redeliver the persisted records, not fabricate local outcomes.
        VillageDeliveryOutbox.objects.filter(command=command).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        recovered = OutboxService.claim_pending("10011", limit=10)
        self.assertEqual(
            {item["delivery_id"] for item in recovered},
            {item["delivery_id"] for item in initial},
        )
        self.assertEqual(VillageActionReceipt.objects.filter(command=command).count(), 1)
        self.assertEqual(VillageWorldEvent.objects.filter(command_id=command.command_id).count(), 1)

    def test_bootstrap_restart_terminalizes_ttl_and_world_expiry_via_outbox(self):
        """Env crash after accept cannot leave old Hub timestamps stuck in Unity.

        This uses a fresh Bootstrap read as the process-restart boundary.  Both
        expiry modes must become Backend FAILED facts before the snapshot is
        handed to Env; no command remains for a client-side synthetic Outcome.
        """
        _ttl_content, ttl_command = self._accept("10011")
        _world_content, world_command = self._accept(
            "10012", expires_world_minute=self.time_state.world_minute
        )
        NpcCommandState.objects.filter(command_id=ttl_command.command_id).update(
            received_at_ms=int(time.time() * 1000) - ttl_command.ttl_ms - 1
        )
        self.time_state.world_minute += 1
        self.time_state.time_revision += 1
        self.time_state.save(update_fields=["world_minute", "time_revision"])

        # A new Env process reads only persisted Backend state.
        restarted_bootstrap = BootstrapService.get_bootstrap()
        restored_ids = {
            item["content"]["id"] for item in restarted_bootstrap["commands"]
        }
        self.assertNotIn(str(ttl_command.command_id), restored_ids)
        self.assertNotIn(str(world_command.command_id), restored_ids)

        for command in (ttl_command, world_command):
            command.refresh_from_db()
            self.assertEqual(command.status, "FAILED")
            self.assertEqual(command.result_code, ResultCode.MESSAGE_EXPIRED)
            receipt = VillageActionReceipt.objects.get(command=command)
            self.assertFalse(receipt.success)
            self.assertEqual(receipt.result_code, ResultCode.MESSAGE_EXPIRED)
            self.assertEqual(
                VillageWorldEvent.objects.filter(command_id=command.command_id).count(), 1
            )
            deliveries = list(VillageDeliveryOutbox.objects.filter(command=command))
            self.assertEqual(len(deliveries), 2)
            claimed = OutboxService.claim_pending(command.sender_id, limit=10)
            self.assertEqual(len(claimed), 2)
            self.assertEqual(
                {item["delivery_id"] for item in claimed},
                {item["envelope"]["id"] for item in claimed},
            )

    def test_wuxi_village_five_npcs_run_for_three_consecutive_game_days(self):
        """Exercise the production accept/start/complete and authoritative tick path.

        The 4,320 individual minute CAS writes intentionally avoid a synthetic
        clock jump: this is the recovery-relevant shape Env uses in production.
        """
        start = GlobalGameTimeState.world_minute_from_calendar(2027, 3, 1, 0, 0)
        self.time_state.world_minute = start
        self.time_state.time_revision = 0
        self.time_state.save()

        for _day in range(3):
            for npc_id in FORMAL_NPC_IDS:
                command, receipt = self._start_and_complete(npc_id)
                command.refresh_from_db()
                self.assertEqual(command.status, "SUCCEEDED")
                self.assertTrue(receipt.success)

            for _minute in range(1440):
                self.time_state.refresh_from_db()
                ok, code, _ = TimeService.advance_time(
                    self.time_state.world_minute + 1,
                    self.time_state.time_revision,
                    uuid.uuid4(),
                )
                self.assertTrue(ok, code)

        self.time_state.refresh_from_db()
        self.assertEqual(self.time_state.world_minute, start + 3 * 1440)
        self.assertEqual(self.time_state.time_revision, 3 * 1440)
        self.assertEqual(
            VillageActionReceipt.objects.filter(action_kind="MOVE", success=True).count(),
            15,
        )
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="NPC_ACTION_COMPLETED").count(), 15)
        self.assertEqual(VillageDeliveryOutbox.objects.filter(status="PENDING").count(), 30)
        for runtime in self.runtimes.values():
            runtime.refresh_from_db()
            self.assertEqual(runtime.action_status, "IDLE")
            self.assertIsNone(runtime.active_command_id)
