import time
import uuid

from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.dialogue_service import DialogueService
from village.models import (
    NpcActionReservation,
    NpcDialogueSession,
    NpcProfile,
    NpcRelationshipChange,
    NpcRuntimeState,
    NpcSocialDailyCounter,
    VillageLocationDefinition,
    VillageSimulationConfig,
    VillageWorldEvent,
)
from village.phase2_services import TickService, runtime_summary
from village.phase4_visibility import _recipients
from village.runtime_services import BootstrapService, CommandService, ResultCode


class DialogueServiceTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(
            config_key="default", defaults={"village_needs_enabled": True}
        )
        VillageLocationDefinition.objects.update_or_create(
            location_code="SQUARE", defaults={
                "display_name": "Square", "center_anchor_code": "square", "location_type": "public"
            }
        )
        self.npcs = []
        for agent_id in ("10011", "10012"):
            npc, _ = NpcDefinition.objects.update_or_create(
                agent_id=agent_id, defaults={"name": agent_id, "is_test_only": False}
            )
            NpcProfile.objects.update_or_create(npc=npc, defaults={
                "display_name": agent_id, "occupation": "test",
                "home_location_code": "SQUARE", "work_location_code": "SQUARE",
                "social_location_code": "SQUARE",
                "capabilities": ["MOVE", "WAIT", "GET_RULE"],
            })
            NpcRuntimeState.objects.update_or_create(npc=npc, defaults={
                "current_location_code": "SQUARE", "needs_world_minute": 0,
                "action_status": "IDLE", "current_action": "",
            })
            self.npcs.append(npc)
        self.time = GlobalGameTimeState.get_instance()
        self.time.world_minute = 0
        self.time.time_revision = 1
        self.time.save(update_fields=["world_minute", "time_revision"])

    def test_consecutive_speech_projection_and_social_settlement(self):
        started = DialogueService.start(
            "10011", "10012", "harvest", conversation_id="cid-dialogue-test-1"
        )
        self.assertTrue(started["success"], started)
        conversation_id = started["conversation_id"]
        replay = DialogueService.start(
            "10012", "10011", conversation_id="cid-dialogue-test-1"
        )
        self.assertEqual((replay["conversation_id"], replay["reused"]), (conversation_id, True))
        self.assertEqual(runtime_summary("10012")["action_status"], "DIALOGUING")
        self.assertEqual(runtime_summary("10011")["available_actions"], ["GET_RULE"])

        self.assertEqual(DialogueService.speak(conversation_id, "10011", "one")["result_code"], ResultCode.DIALOGUE_SPOKEN)
        self.assertEqual(DialogueService.speak(conversation_id, "10011", "two")["result_code"], ResultCode.DIALOGUE_SPOKEN)
        self.assertEqual(DialogueService.speak(conversation_id, "10012", "reply")["result_code"], ResultCode.DIALOGUE_SPOKEN)
        self.time.world_minute = 30
        self.time.save(update_fields=["world_minute"])

        completed = DialogueService.complete(conversation_id, "10011")
        self.assertEqual((completed["valid"], completed["duration_minutes"], completed["social_multiplier_percent"]), (True, 30, 100))
        session = NpcDialogueSession.objects.get(pk=conversation_id)
        self.assertEqual([message["speaker_npc_id"] for message in session.messages], ["10011", "10011", "10012"])
        self.assertEqual(NpcSocialDailyCounter.objects.get().socialize_count, 1)
        self.assertEqual(VillageWorldEvent.objects.filter(event_type="DIALOGUE_SETTLED").count(), 1)
        self.assertEqual(_recipients(VillageWorldEvent.objects.get(event_type="DIALOGUE_SETTLED")), [])
        self.assertEqual(NpcRelationshipChange.objects.count(), 2)
        self.assertEqual(runtime_summary("10011")["action_status"], "IDLE")

    def test_start_does_not_use_logical_location_as_distance(self):
        NpcRuntimeState.objects.filter(npc=self.npcs[1]).update(current_location_code="OTHER")

        started = DialogueService.start("10011", "10012", conversation_id="cid-dialogue-test")

        self.assertTrue(started["success"], started)
        self.assertEqual(started["result_code"], ResultCode.DIALOGUE_STARTED)
        self.assertTrue(started["conversation_id"])

    def test_dialogue_settlement_adds_delta_after_tick_base_once(self):
        started = DialogueService.start("10011", "10012", conversation_id="cid-dialogue-test")
        conversation_id = started["conversation_id"]
        DialogueService.speak(conversation_id, "10011", "hello")
        DialogueService.speak(conversation_id, "10012", "hi")
        result = TickService.advance({
            "world_minute": 1, "expected_time_revision": 1,
            "request_id": "6d1b6b22-6a2c-45cc-b5b6-f1d9d73a4b01",
        })
        self.assertTrue(result["success"], result)
        self.time.world_minute = 30
        self.time.save(update_fields=["world_minute"])

        self.assertTrue(DialogueService.complete(conversation_id, "10011")["success"])
        for npc in self.npcs:
            runtime = NpcRuntimeState.objects.get(npc=npc)
            self.assertEqual((runtime.needs_world_minute, runtime.hunger_fp), (30, 220))
            self.assertEqual(runtime.social_fp, 120)

    def test_expired_one_sided_dialogue_fills_idle_without_reward(self):
        started = DialogueService.start("10011", "10012", conversation_id="cid-dialogue-test")
        DialogueService.speak(started["conversation_id"], "10011", "hello", end_turn=True)
        rejected = DialogueService.speak(started["conversation_id"], "10011", "too late")
        self.assertEqual(rejected["result_code"], ResultCode.DIALOGUE_ALREADY_ENDED_SELF)
        self.time.world_minute = 480
        self.time.save(update_fields=["world_minute"])

        self.assertEqual(DialogueService.expire_active(480), 1)
        session = NpcDialogueSession.objects.get(pk=started["conversation_id"])
        self.assertEqual((session.status, session.valid_for_social_needs), ("EXPIRED", False))
        self.assertFalse(NpcSocialDailyCounter.objects.exists())
        for npc in self.npcs:
            runtime = NpcRuntimeState.objects.get(npc=npc)
            self.assertEqual((runtime.needs_world_minute, runtime.social_fp), (480, 360))

    def test_recovery_expires_unfinished_dialogue_immediately(self):
        started = DialogueService.start("10011", "10012", conversation_id="cid-dialogue-test")
        self.time.world_minute = 30
        self.time.save(update_fields=["world_minute"])

        self.assertEqual(DialogueService.expire_active(30, include_unexpired=True), 1)

        session = NpcDialogueSession.objects.get(pk=started["conversation_id"])
        self.assertEqual((session.status, session.valid_for_social_needs), ("EXPIRED", False))
        self.assertIsNone(session.reservation.active_lock_key)
        for npc in self.npcs:
            runtime = NpcRuntimeState.objects.get(npc=npc)
            self.assertEqual(runtime.needs_world_minute, 30)

    def test_passive_bootstrap_keeps_dialogue_but_recovery_expires_it(self):
        started = DialogueService.start("10011", "10012", conversation_id="cid-bootstrap-dialogue")
        self.time.world_minute = 30
        self.time.save(update_fields=["world_minute"])

        BootstrapService.get_bootstrap(recovery=False)
        self.assertEqual(
            NpcDialogueSession.objects.get(pk=started["conversation_id"]).status, "ACTIVE"
        )
        BootstrapService.get_bootstrap(recovery=True)
        session = NpcDialogueSession.objects.get(pk=started["conversation_id"])
        self.assertEqual((session.status, session.valid_for_social_needs), ("EXPIRED", False))

    def test_natural_expiry_rewards_a_dialogue_when_both_participants_spoke(self):
        started = DialogueService.start("10011", "10012", conversation_id="cid-dialogue-test")
        DialogueService.speak(started["conversation_id"], "10011", "hello")
        DialogueService.speak(started["conversation_id"], "10012", "hi")
        self.time.world_minute = 480
        self.time.save(update_fields=["world_minute"])

        self.assertEqual(DialogueService.expire_active(480), 1)

        session = NpcDialogueSession.objects.get(pk=started["conversation_id"])
        self.assertEqual((session.status, session.valid_for_social_needs), ("EXPIRED", True))
        self.assertEqual(session.social_multiplier_percent, 100)

    def test_late_expiry_catches_needs_up_to_authoritative_time(self):
        DialogueService.start("10011", "10012", conversation_id="cid-dialogue-test-late")
        self.time.world_minute = 485
        self.time.save(update_fields=["world_minute"])

        self.assertEqual(DialogueService.expire_active(485), 1)

        for npc in self.npcs:
            self.assertEqual(NpcRuntimeState.objects.get(npc=npc).needs_world_minute, 485)

    def test_start_requires_caller_supplied_conversation_id(self):
        missing = DialogueService.start("10011", "10012")
        self.assertEqual(missing["result_code"], ResultCode.INVALID_COMMAND_SCHEMA)
        too_long = DialogueService.start("10011", "10012", conversation_id="x" * 129)
        self.assertEqual(too_long["result_code"], ResultCode.INVALID_COMMAND_SCHEMA)
        self.assertFalse(NpcDialogueSession.objects.exists())

    def test_dialogue_allows_npc_busy_with_daily_action(self):
        NpcRuntimeState.objects.filter(npc=self.npcs[0]).update(
            action_status="ACTING", current_action="COOK", health_fp=50
        )
        started = DialogueService.start("10011", "10012", conversation_id="cid-dialogue-acting")
        self.assertTrue(started["success"], started)

    def test_dialogue_rejects_moving_npc(self):
        NpcRuntimeState.objects.filter(npc=self.npcs[0]).update(
            action_status="MOVING", current_action="MOVE"
        )
        started = DialogueService.start(
            "10011", "10012", conversation_id="cid-dialogue-moving"
        )
        self.assertEqual(started["result_code"], ResultCode.SOCIAL_TARGET_BUSY)
        self.assertFalse(NpcDialogueSession.objects.exists())

    def test_active_dialogue_blocks_move_but_not_other_commands(self):
        DialogueService.start("10011", "10012", conversation_id="cid-command-lock")
        runtime = NpcRuntimeState.objects.get(npc=self.npcs[0])

        def accept(action, params, duration=None):
            content = {
                "schema_version": "village.command.v1",
                "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()),
                "name": action, "params": params, "queue_mode": "STRICT",
                "expected_state_version": runtime.state_version, "ttl_ms": 30000,
            }
            if duration is not None:
                content["duration_minutes"] = duration
            return CommandService.accept_command(
                str(uuid.uuid4()), "10011", "env-001", content,
                int(time.time() * 1000),
            )[:2]

        self.assertEqual(accept("MOVE", {"location_code": "SQUARE"}),
                         (False, ResultCode.SOCIAL_TARGET_BUSY))
        self.assertEqual(accept("WAIT", {}, 1),
                         (True, ResultCode.COMMAND_ACCEPTED))

    def test_dialogue_coexists_with_held_daily_reservation(self):
        res = NpcActionReservation.objects.create(
            action_kind="HUNT", location_code="SQUARE",
            active_lock_key="participants:10011", status="HELD",
            acquired_world_minute=0, expires_world_minute=500,
        )
        res.participants.set([self.npcs[0]])
        started = DialogueService.start("10011", "10012", conversation_id="cid-dialogue-held-reservation")
        self.assertTrue(started["success"], started)

    def test_dialogue_rejects_sleeping_npc(self):
        NpcRuntimeState.objects.filter(npc=self.npcs[0]).update(action_status="SLEEPING")
        started = DialogueService.start("10011", "10012", conversation_id="cid-dialogue-sleeping")
        self.assertEqual(started["result_code"], ResultCode.SOCIAL_TARGET_BUSY)
        self.assertFalse(NpcDialogueSession.objects.exists())
