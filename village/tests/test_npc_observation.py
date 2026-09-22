import uuid
from datetime import date

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType
from marketplace.models import MarketListing, MarketListingSource
from village.models import (
    NpcCommandState,
    NpcCommitment,
    NpcProfile,
    NpcRelationship,
    NpcRuntimeState,
    VillageActionReceipt,
    VillageLocationDefinition,
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
        VillageLocationDefinition.objects.update_or_create(
            location_code="MARKET_STREET",
            defaults={
                "display_name": "市场街", "center_anchor_code": "MARKET_STREET",
                "location_type": "public", "is_public": True, "is_active": True,
            },
        )
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
        runtime.current_action = "MOVE"
        runtime.action_status = "ACTING"
        active_command = NpcCommandState.objects.create(
            command_id=uuid.uuid4(), message_id=uuid.uuid4(), request_id=uuid.uuid4(),
            npc=self.npcs["10011"], sender_id="10011", recipient_id="WuxiVillage",
            kind="MOVE", sanitized_parameters={"location_code": "MARKET_STREET"},
            payload_sha256="a" * 64, status="RUNNING", received_world_minute=499,
            started_world_minute=499,
        )
        runtime.active_command_id = active_command.command_id
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
        Item.objects.create(item_id=1030, item_name="玉米", item_type=ItemType.FOOD)
        bag, _ = NpcGlobalInventory.objects.get_or_create(npc=self.npcs["10011"])
        food = NpcGlobalInventorySlot.objects.create(
            bag=bag, slot_index=0, item_id=1030, item_amount=2,
        )
        listing = MarketListing.objects.create(
            source_type=MarketListingSource.SYSTEM, item_id=1030, item_name_snapshot="玉米",
            total_amount=10, remaining_amount=8, price=8,
        )
        command_rows = [
            ("SLEEP", {"duration_minutes": 10}, 10),
            ("REST", {"duration_minutes": 20}, 20),
            ("EAT", {"source": "PERSONAL_INVENTORY", "food_instance_id": str(food.instance_id)}, None),
            ("BUY_FROM_MARKETPLACE", {"listing_id": listing.pk, "amount": 2}, None),
        ]
        for minute, (action, parameters, duration) in enumerate(command_rows):
            command = NpcCommandState.objects.create(
                command_id=uuid.uuid4(), message_id=uuid.uuid4(), request_id=uuid.uuid4(),
                npc=self.npcs["10011"], sender_id="10011", recipient_id="WuxiVillage",
                kind=action, sanitized_parameters=parameters, duration_minutes=duration,
                payload_sha256=f"{minute:064d}", status="SUCCEEDED",
                completed_world_minute=minute,
            )
            VillageActionReceipt.objects.create(
                request_id=command.request_id, command=command, npc=self.npcs["10011"],
                action_kind=action, success=True, result_code="COMMAND_COMPLETED",
                world_minute=minute, time_revision=7, state_version=minute,
            )
        recorded = self.api_client(self.env, env=True).post(
            "/api/village/runtime/instruction-outcome",
            {
                "request_id": "agent-query-inventory-1", "npc_id": "10011",
                "action": "INSPECT_INVENTORY", "success": True,
                "result_code": "QUERY_OK", "world_minute": 501,
                "time_revision": 7, "state_version": 4, "params": {},
                "data": {
                    "money": 18,
                    "items": [
                        {"item_id": 1030, "quantity": 2},
                        {"item_id": 1056, "quantity": 3},
                    ],
                },
            },
            format="json",
        )
        self.assertEqual((recorded.status_code, recorded.data["resultCode"]), (200, "RECORDED"))
        NpcCommandState.objects.create(
            command_id=uuid.uuid4(), message_id=uuid.uuid4(), request_id=uuid.uuid4(),
            npc=self.npcs["10011"], sender_id="10011", recipient_id="WuxiVillage",
            kind="HARVEST", sanitized_parameters={"plot_code": "FARM_PLOT_A"},
            payload_sha256="c" * 64, status="QUEUED", received_world_minute=502,
        )

        response = self.api_client(self.player).get("/api/village/public/npcs/10011/observation")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data["public_needs"]["hunger"], 32.1)
        self.assertEqual(data["public_needs"]["health"], 87.6)
        self.assertEqual(data["world_minute"], 500)
        self.assertEqual(data["public_status"]["summary"], "移动至市场街")
        self.assertEqual(data["subjective_goal"]["summary"], "扩建农田")
        self.assertEqual(
            [row["name"] for row in data["recent_commands"][:5]],
            ["HARVEST", "INSPECT_INVENTORY", "UPDATE_SUBJECTIVE_GOAL", "MOVE", "BUY_FROM_MARKETPLACE"],
        )
        self.assertEqual(data["recent_commands"][7]["name"], "SLEEP")
        self.assertEqual(data["recent_commands"][0]["status"], "WAITING")
        self.assertIn("已等待", data["recent_commands"][0]["detail"])
        self.assertEqual(data["recent_commands"][0]["received_world_minute"], 502)
        self.assertIsNone(data["recent_commands"][0]["completed_world_minute"])
        self.assertEqual(data["recent_commands"][1]["summary"], "查看背包")
        self.assertEqual(data["recent_commands"][1]["category"], "QUERY")
        for row in data["recent_commands"]:
            self.assertTrue(row["summary"].strip(), row["name"])
            if row["name"] != "INSPECT_INVENTORY":
                self.assertEqual(row["category"], "EXECUTION", row["name"])
        self.assertEqual(data["recent_commands"][1]["detail"], "金币18，物品2种/5件。")
        self.assertEqual(data["recent_commands"][3]["summary"], "移动至市场街")
        self.assertEqual(data["recent_commands"][3]["status"], "RUNNING")
        self.assertEqual(data["recent_commands"][3]["started_world_minute"], 499)
        self.assertEqual(data["recent_commands"][3]["detail"], "已执行1分钟。")
        self.assertEqual(data["recent_commands"][4]["summary"], "购买玉米 ×2")
        self.assertIn("数量2", data["recent_commands"][4]["detail"])
        self.assertEqual(data["recent_commands"][5]["summary"], "进食玉米（个人背包）")
        self.assertTrue(all(len(row["detail"]) <= 40 for row in data["recent_commands"]))
        self.assertNotIn("COMMAND_COMPLETED", "".join(row["detail"] for row in data["recent_commands"]))
        self.assertNotIn("sanitized_parameters", data["recent_commands"][0])
        self.assertEqual(data["relationships"][0]["other_display_name"], "杰克")
        self.assertEqual(data["commitments"][0]["commitment_type"], "MEETING")
        self.assertNotIn("target", data["commitments"][0])

        self.assertEqual(self.api_client(self.env, env=True).get(
            "/api/village/public/npcs/10011/observation"
        ).status_code, 403)

    def test_command_detail_formats_absolute_world_minute_as_calendar_date(self):
        minute = (date(2026, 3, 8).toordinal() - 1) * 1440 + 9 * 60 + 5
        command = NpcCommandState.objects.create(
            command_id=uuid.uuid4(), message_id=uuid.uuid4(), request_id=uuid.uuid4(),
            npc=self.npcs["10011"], sender_id="10011", recipient_id="WuxiVillage",
            kind="REST", sanitized_parameters={}, payload_sha256="d" * 64,
            status="FAILED", received_world_minute=minute, completed_world_minute=minute,
        )
        VillageActionReceipt.objects.create(
            request_id=command.request_id, command=command, npc=self.npcs["10011"],
            action_kind="REST", success=False, result_code="UNCLASSIFIED_RESULT",
            world_minute=minute, time_revision=7,
        )

        response = self.api_client(self.player).get("/api/village/public/npcs/10011/observation")
        detail = response.data["data"]["recent_commands"][0]["detail"]
        self.assertIn("03月08日 09:05", detail)
        self.assertNotIn("739", detail)

    def test_public_observation_uses_command_kind_for_legacy_receipts(self):
        command = NpcCommandState.objects.create(
            command_id=uuid.uuid4(), message_id=uuid.uuid4(), request_id=uuid.uuid4(),
            npc=self.npcs["10011"], sender_id="10011", recipient_id="WuxiVillage",
            kind="EAT", sanitized_parameters={"source": "PERSONAL_INVENTORY"},
            payload_sha256="b" * 64, status="SUCCEEDED", duration_minutes=30,
            completed_world_minute=499,
        )
        VillageActionReceipt.objects.create(
            request_id=command.request_id, command=command, npc=self.npcs["10011"],
            action_kind="ECONOMY_EAT", success=True, result_code="OK",
            world_minute=499, time_revision=7,
        )

        response = self.api_client(self.player).get("/api/village/public/npcs/10011/observation")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["recent_commands"][0]["name"], "EAT")

    def test_speech_outcome_appears_in_command_history(self):
        recorded = self.api_client(self.env, env=True).post(
            "/api/village/runtime/instruction-outcome",
            {
                "request_id": "speech-activity-1", "npc_id": "10011",
                "action": "SPEECH", "success": True,
                "result_code": "DIALOGUE_SPOKEN", "world_minute": 501,
                "time_revision": 7, "state_version": 4,
                "params": {"conversation_id": "chat-1", "text": "你好", "end_turn": False},
            },
            format="json",
        )
        self.assertEqual(recorded.status_code, 200)

        response = self.api_client(self.player).get("/api/village/public/npcs/10011/observation")
        command = response.data["data"]["recent_commands"][0]
        self.assertEqual((command["name"], command["status"]), ("SPEECH", "SUCCEEDED"))
        self.assertEqual(command["summary"], "对话发言：“你好”")

    def test_retired_dialogue_receipts_remain_readable(self):
        for index, action in enumerate(("START_DIALOGUE", "SPEAK")):
            VillageActionReceipt.objects.create(
                request_id=uuid.uuid4(), npc=self.npcs["10011"], action_kind=action,
                success=True, result_code="DIALOGUE_SPOKEN", world_minute=510 + index,
                time_revision=7, response_data={"parameters": {"text": "历史发言"}},
            )
        response = self.api_client(self.player).get("/api/village/public/npcs/10011/observation")
        names = {row["name"] for row in response.data["data"]["recent_commands"]}
        self.assertTrue({"START_DIALOGUE", "SPEAK"}.issubset(names))
