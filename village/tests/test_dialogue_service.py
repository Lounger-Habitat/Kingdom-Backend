from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.dialogue_service import DialogueService
from village.models import (
    NpcDialogueSession,
    NpcProfile,
    NpcRuntimeState,
    NpcSocialDailyCounter,
    VillageLocationDefinition,
    VillageSimulationConfig,
)
from village.phase2_services import TickService, runtime_summary
from village.runtime_services import ResultCode


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
                "social_location_code": "SQUARE", "capabilities": ["START_DIALOGUE", "SPEAK"],
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
        started = DialogueService.start("10011", "10012", "harvest")
        self.assertTrue(started["success"], started)
        conversation_id = started["conversation_id"]
        replay = DialogueService.start("10012", "10011")
        self.assertEqual((replay["conversation_id"], replay["reused"]), (conversation_id, True))
        self.assertEqual(runtime_summary("10012")["action_status"], "DIALOGUING")
        self.assertEqual(runtime_summary("10011")["available_actions"], ["SPEAK"])

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
        self.assertEqual(runtime_summary("10011")["action_status"], "IDLE")

    def test_start_does_not_use_logical_location_as_distance(self):
        NpcRuntimeState.objects.filter(npc=self.npcs[1]).update(current_location_code="OTHER")

        started = DialogueService.start("10011", "10012")

        self.assertTrue(started["success"], started)
        self.assertEqual(started["result_code"], ResultCode.DIALOGUE_STARTED)
        self.assertTrue(started["conversation_id"])

    def test_dialogue_settlement_adds_delta_after_tick_base_once(self):
        started = DialogueService.start("10011", "10012")
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
        started = DialogueService.start("10011", "10012")
        DialogueService.speak(started["conversation_id"], "10011", "hello", end_turn=True)
        rejected = DialogueService.speak(started["conversation_id"], "10011", "too late")
        self.assertEqual(rejected["result_code"], ResultCode.DIALOGUE_ALREADY_ENDED_SELF)
        self.time.world_minute = 120
        self.time.save(update_fields=["world_minute"])

        self.assertEqual(DialogueService.expire_active(120), 1)
        session = NpcDialogueSession.objects.get(pk=started["conversation_id"])
        self.assertEqual((session.status, session.valid_for_social_needs), ("EXPIRED", False))
        self.assertFalse(NpcSocialDailyCounter.objects.exists())
        for npc in self.npcs:
            runtime = NpcRuntimeState.objects.get(npc=npc)
            self.assertEqual((runtime.needs_world_minute, runtime.social_fp), (120, 240))

    def test_recovery_expires_unfinished_dialogue_immediately(self):
        started = DialogueService.start("10011", "10012")
        self.time.world_minute = 30
        self.time.save(update_fields=["world_minute"])

        self.assertEqual(DialogueService.expire_active(30, include_unexpired=True), 1)

        session = NpcDialogueSession.objects.get(pk=started["conversation_id"])
        self.assertEqual((session.status, session.valid_for_social_needs), ("EXPIRED", False))
        self.assertIsNone(session.reservation.active_lock_key)
        for npc in self.npcs:
            runtime = NpcRuntimeState.objects.get(npc=npc)
            self.assertEqual(runtime.needs_world_minute, 30)

    def test_natural_expiry_rewards_a_dialogue_when_both_participants_spoke(self):
        started = DialogueService.start("10011", "10012")
        DialogueService.speak(started["conversation_id"], "10011", "hello")
        DialogueService.speak(started["conversation_id"], "10012", "hi")
        self.time.world_minute = 120
        self.time.save(update_fields=["world_minute"])

        self.assertEqual(DialogueService.expire_active(120), 1)

        session = NpcDialogueSession.objects.get(pk=started["conversation_id"])
        self.assertEqual((session.status, session.valid_for_social_needs), ("EXPIRED", True))
        self.assertEqual(session.social_multiplier_percent, 100)

    def test_late_expiry_catches_needs_up_to_authoritative_time(self):
        DialogueService.start("10011", "10012")
        self.time.world_minute = 125
        self.time.save(update_fields=["world_minute"])

        self.assertEqual(DialogueService.expire_active(125), 1)

        for npc in self.npcs:
            self.assertEqual(NpcRuntimeState.objects.get(npc=npc).needs_world_minute, 125)
