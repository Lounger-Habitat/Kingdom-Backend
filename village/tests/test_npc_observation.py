import uuid

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.models import (
    NpcCommandState,
    NpcCommitment,
    NpcProfile,
    NpcRelationship,
    NpcRuntimeState,
    VillageSimulationConfig,
    VillageWorldEvent,
)


class NpcObservationTests(TestCase):
    def setUp(self):
        self.player = GameAccount.objects.create(username="observation-player", password="unused")
        self.env = GameAccount.objects.create(username="observation-env", password="unused")
        VillageSimulationConfig.objects.update_or_create(
            config_key="default", defaults={"village_presentation_enabled": True}
        )
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 500
        self.clock.time_revision = 7
        self.clock.save(update_fields=["world_minute", "time_revision"])
        self.npcs = {}
        for npc_id, name in (("10011", "比尔"), ("10012", "杰克")):
            npc, _ = NpcDefinition.objects.get_or_create(
                agent_id=npc_id, defaults={"name": name, "is_test_only": False}
            )
            npc.name = name
            npc.is_test_only = False
            npc.save(update_fields=["name", "is_test_only"])
            self.npcs[npc_id] = npc
            NpcProfile.objects.update_or_create(npc=npc, defaults={
                "display_name": name, "occupation": "村民",
                "home_location_code": "HOME", "work_location_code": "WORK",
                "social_location_code": "SQUARE", "is_active": True,
            })
            NpcRuntimeState.objects.get_or_create(npc=npc)

    def api_client(self, account, *, env=False):
        token = AccessToken.for_user(account)
        if env:
            token["scope"] = "env_server"
            token["aud"] = "kingdom-env"
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def goal_envelope(self, *, request_id=None, goal_id=None, version=0, summary="扩建农田"):
        return {
            "envelope_id": str(uuid.uuid4()),
            "sender": "10011",
            "recipient": "WuxiVillage",
            "timestamp": 1,
            "content": {
                "schema_version": "village.command.v1",
                "id": str(uuid.uuid4()),
                "request_id": str(request_id or uuid.uuid4()),
                "name": "UPDATE_SUBJECTIVE_GOAL",
                "queue_mode": "STRICT",
                "params": {
                    "goal_id": str(goal_id or uuid.uuid4()),
                    "summary": summary,
                    "status": "ACTIVE",
                    "visibility": "PUBLIC",
                    "expected_goal_version": version,
                },
            },
        }

    def test_goal_update_is_idempotent_and_versioned(self):
        payload = self.goal_envelope()
        client = self.api_client(self.env, env=True)
        first = client.post("/api/village/runtime/subjective-goal", payload, format="json")
        self.assertEqual((first.status_code, first.data["resultCode"]), (200, "GOAL_UPDATED"))
        self.assertEqual(first.data["data"]["goal_version"], 1)

        replay = client.post("/api/village/runtime/subjective-goal", payload, format="json")
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.data["data"]["replayed"])

        stale = client.post(
            "/api/village/runtime/subjective-goal", self.goal_envelope(version=0), format="json"
        )
        self.assertEqual((stale.status_code, stale.data["resultCode"]), (409, "GOAL_VERSION_CONFLICT"))
        self.assertEqual(stale.data["data"]["goal_version"], 1)

    def test_public_observation_contains_only_requested_public_sections(self):
        runtime = NpcRuntimeState.objects.get(npc=self.npcs["10011"])
        runtime.hunger_fp = 321
        runtime.health_fp = 876
        runtime.current_action = "EAT"
        runtime.action_status = "ACTING"
        runtime.save()
        self.api_client(self.env, env=True).post(
            "/api/village/runtime/subjective-goal", self.goal_envelope(), format="json"
        )
        NpcRelationship.objects.create(
            from_npc=self.npcs["10011"], to_npc=self.npcs["10012"], affinity=35
        )
        event = VillageWorldEvent.objects.create(
            dedupe_key=f"observation:{uuid.uuid4()}", event_type="COMMITMENT_CREATED",
            world_minute=500, time_revision=7, visibility="PUBLIC",
        )
        NpcCommitment.objects.create(
            commitment_type="MEETING", promisor_npc=self.npcs["10011"],
            beneficiary_npc=self.npcs["10012"], due_world_minute=600,
            source_request_id=uuid.uuid4(), created_event=event, created_world_minute=500,
        )
        for minute in range(4):
            NpcCommandState.objects.create(
                command_id=uuid.uuid4(), message_id=uuid.uuid4(), request_id=uuid.uuid4(),
                npc=self.npcs["10011"], sender_id="10011", recipient_id="WuxiVillage",
                kind=f"ACTION_{minute}", payload_sha256=f"{minute:064d}", status="SUCCEEDED",
                completed_world_minute=minute,
            )

        response = self.api_client(self.player).get("/api/village/public/npcs/10011/observation")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data["public_needs"]["hunger"], 32.1)
        self.assertEqual(data["public_needs"]["health"], 87.6)
        self.assertEqual(data["subjective_goal"]["summary"], "扩建农田")
        self.assertEqual([row["name"] for row in data["recent_commands"]], ["ACTION_3", "ACTION_2", "ACTION_1"])
        self.assertEqual(data["relationships"][0]["other_display_name"], "杰克")
        self.assertEqual(data["commitments"][0]["commitment_type"], "MEETING")
        self.assertNotIn("target", data["commitments"][0])

        self.assertEqual(self.api_client(self.env, env=True).get(
            "/api/village/public/npcs/10011/observation"
        ).status_code, 403)
