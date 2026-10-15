from copy import deepcopy
from types import SimpleNamespace
import time
import uuid

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition
from village.models import NpcCommandState, NpcProfile, NpcRuntimeState, NpcDailyRecord, NpcSubjectiveGoalState, VillageActionDurationConfig, VillageActionTask, VillageSimulationConfig
from village.runtime_services import BootstrapService, CommandService, OutboxService
from village.star_actions import accept_action, task_context
from village.views import commands_accept, star_action_status


@override_settings(VILLAGE_FAST_COMMAND_INTEGRATION=False)
class StarActionTests(TestCase):
    def setUp(self):
        self.npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        NpcProfile.objects.update_or_create(npc=self.npc, defaults={"display_name": "Bill", "is_active": True,
            "home_location_code": "BILL_HOME", "work_location_code": "IRONSMITH", "social_location_code": "VILLAGE_SQUARE",
            "capabilities": ["MOVE", "REST", "SLEEP", "WRITE_DIARY", "UPDATE_SUBJECTIVE_GOAL", "GET_RULE"]})
        NpcRuntimeState.objects.update_or_create(npc=self.npc, defaults={"current_location_code": "BILL_HOME"})
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={"village_needs_enabled": True})
        GlobalGameTimeState.get_instance()

    def message(self, action_id="move-001", name="MOVE", params=None, sender="10011", environment="WuxiVillage"):
        return {"id": str(uuid.uuid4()), "timestamp": int(time.time() * 1000), "type": "message", "sender": sender,
                "recipient": environment, "payload": {"type": "action", "content": {
                    "id": action_id, "name": name, "params": params if params is not None else {"location_code": "BILL_HOME"}}}}

    def test_raw_move_and_replay_preserve_content_and_create_one_private_task(self):
        original = self.message()
        before = deepcopy(original)
        ok, code, task, replayed = accept_action(original)
        self.assertTrue(ok, code)
        self.assertFalse(replayed)
        self.assertEqual(original, before)
        self.assertEqual(task.envelope, before)
        self.assertEqual(task.command.command_id, task.pk)
        self.assertEqual(task.command.request_id, task.pk)
        self.assertEqual(task.command.agent_execution, {})
        self.assertEqual(task.command.schema_version, "")
        retry = deepcopy(original)
        retry["id"] = str(uuid.uuid4())
        retry["timestamp"] += 1
        again = accept_action(retry)
        self.assertTrue(again[0], again[1])
        self.assertTrue(again[3])
        self.assertEqual(again[2].pk, task.pk)
        self.assertEqual(VillageActionTask.objects.filter(sender="10011", action_id="move-001").count(), 1)
        self.assertEqual(NpcCommandState.objects.filter(npc=self.npc).count(), 1)
        self.assertEqual(task_context(task)["action"]["payload"]["content"], before["payload"]["content"])

    def test_http_entry_and_read_only_reconciliation_keep_the_original_action(self):
        original = self.message("move/http-001")
        request = APIRequestFactory().post("/", original, format="json")
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        response = commands_accept(request)
        self.assertTrue(response.data["success"], response.data)
        self.assertEqual(response.data["data"]["action"], original)
        request = APIRequestFactory().get("/", {"environment": "WuxiVillage"})
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        status = star_action_status(request, "10011", "move/http-001")
        self.assertEqual(status.data["data"]["task_id"], response.data["data"]["task_id"])
        self.assertEqual(NpcCommandState.objects.filter(npc=self.npc).count(), 1)
        wrong = star_action_status(request, "10012", "move/http-001")
        self.assertEqual(wrong.status_code, 404)

    def test_duration_defaults_are_task_data_and_retries_keep_the_first_resolution(self):
        VillageActionDurationConfig.objects.update_or_create(action_name="REST", defaults={"duration_minutes": 30})
        original = self.message("rest-001", "REST", {})
        ok, code, task, _ = accept_action(original)
        self.assertTrue(ok, code)
        self.assertEqual(task_context(task)["duration_minutes"], 30)
        self.assertEqual(original["payload"]["content"]["params"], {})
        VillageActionDurationConfig.objects.filter(action_name="REST").update(duration_minutes=50)
        self.assertEqual(task_context(accept_action(original)[2])["duration_minutes"], 30)

    def test_diary_and_goal_updates_need_no_request_id_or_state_version_in_the_action(self):
        diary = self.message("diary-001", "WRITE_DIARY", {"content": "今天回家休息。"})
        ok, code, task, _ = accept_action(diary)
        self.assertTrue(ok, code)
        self.assertEqual(accept_action(diary)[2].result, task.result)
        self.assertEqual(NpcDailyRecord.objects.filter(npc=self.npc, kind="DIARY").count(), 1)
        goal = self.message("goal-001", "UPDATE_SUBJECTIVE_GOAL", {
            "goal_id": str(uuid.uuid4()), "summary": "照顾村里的菜地", "status": "ACTIVE", "visibility": "PUBLIC"})
        self.assertTrue(accept_action(goal)[0])
        self.assertTrue(accept_action(goal)[3])
        self.assertEqual(NpcSubjectiveGoalState.objects.get(npc=self.npc).goal_version, 1)
        self.assertNotIn("expected_goal_version", goal["payload"]["content"]["params"])

    def test_wake_uses_the_original_sleep_action_and_keeps_its_terminal_outbox(self):
        clock = GlobalGameTimeState.get_instance()
        clock.world_minute = 0
        clock.save(update_fields=["world_minute"])
        original = self.message("sleep-001", "SLEEP", {"duration_minutes": 30})
        ok, code, sleeping, _ = accept_action(original)
        self.assertTrue(ok, code)
        ok, code, command = CommandService.start_command(sleeping.command_id, 0)
        self.assertTrue(ok, code)
        clock.refresh_from_db()
        clock.world_minute = 5
        clock.save(update_fields=["world_minute"])
        wake = self.message("wake-001", "WAKE_UP", {"sleep_action_id": "sleep-001"})
        ok, code, task, _ = accept_action(wake)
        self.assertTrue(ok, code)
        self.assertEqual(task.envelope, wake)
        sleeping.command.refresh_from_db()
        self.assertNotEqual(sleeping.command.status, "RUNNING")
        self.assertTrue(accept_action(wake)[3])
        self.assertEqual(BootstrapService.context_for_command(sleeping.command)["action"], original)
        deliveries = OutboxService.claim_pending("10011")
        outcome = next(row for row in deliveries if row["message_kind"] == "OUTCOME")
        self.assertEqual(outcome["action_task"]["action"]["payload"]["content"]["id"], "sleep-001")
        self.assertNotIn("agent_execution", outcome["envelope"]["payload"]["content"])

    def test_changed_content_is_rejected_and_original_owner_is_preserved(self):
        original = self.message()
        task = accept_action(original)[2]
        changed = self.message(params={"location_code": "MARKET"})
        self.assertEqual(accept_action(changed)[:2], (False, "REQUEST_ID_CONFLICT"))
        task.refresh_from_db()
        self.assertEqual(task.envelope, original)
        self.assertEqual(accept_action(self.message("other"))[:2], (False, "NPC_BUSY"))

    def test_old_enrichment_is_rejected_at_the_raw_entry(self):
        for key, value in {"schema_version": "village.command.v1", "request_id": str(uuid.uuid4()),
                           "agent_execution": {}, "queue_mode": "STRICT"}.items():
            envelope = self.message()
            envelope["payload"]["content"][key] = value
            self.assertEqual(accept_action(envelope)[:2], (False, "INVALID_COMMAND_SCHEMA"))
        self.assertFalse(VillageActionTask.objects.filter(sender="10011", action_id="move-001").exists())

    def test_native_store_query_returns_inventory_and_keeps_location_and_replay_rules(self):
        from village.models import VillageStore, VillageStoreListing, VillageStoreInventorySlot
        from items.models import Item, ItemType
        Item.objects.update_or_create(item_id=1051, defaults={"item_name": "测试商店小麦", "item_type": ItemType.INGREDIENT})
        profile = NpcProfile.objects.get(npc=self.npc)
        profile.capabilities += ["INSPECT_STORE_INVENTORY"]
        profile.save()
        store, _ = VillageStore.objects.update_or_create(store_code="GENERAL_STORE", defaults={"location_code": "GENERAL_STORE", "display_name": "杂货铺", "operator_npc": self.npc, "status": "OPEN"})
        invalid = accept_action(self.message("invalid-store", "INSPECT_STORE_INVENTORY", {"store_code": []}))
        self.assertEqual(invalid[:2], (False, "INVALID_QUERY_PARAMS"))
        listing = VillageStoreListing.objects.create(store=store, item_id=1051, unit_price=7, listed_by_npc=self.npc, listed_world_minute=0, created_request_id=uuid.uuid4())
        slot = VillageStoreInventorySlot.objects.create(store=store, listing=listing, item_id=1051, item_amount=3)
        rejected = accept_action(self.message("store-too-far", "INSPECT_STORE_INVENTORY", {"store_code": store.pk}))
        self.assertEqual(rejected[:2], (False, "P3_WRONG_LOCATION"))
        NpcRuntimeState.objects.filter(npc=self.npc).update(current_location_code=store.location_code)
        original = self.message("store-001", "INSPECT_STORE_INVENTORY", {"store_code": store.pk})
        accepted = accept_action(original)
        self.assertTrue(accepted[0], accepted[1])
        result = task_context(accepted[2])["result"]["data"]["store_inventory"]
        self.assertEqual(result["store_code"], store.pk)
        self.assertEqual(result["items"][0]["available_amount"], 3)
        self.assertEqual(result["items"][0]["listing_id"], str(listing.pk))
        self.assertEqual(result["items"][0]["name"], "测试商店小麦")
        self.assertEqual(result["items"][0]["batches"][0]["item_name"], "测试商店小麦")
        self.assertEqual(result["items"][0]["batches"][0]["food_instance_id"], str(slot.instance_id))
        slot.item_amount = 0
        slot.save()
        self.assertEqual(task_context(accept_action(original)[2])["result"]["data"]["store_inventory"], result)

    def test_life_eat_receipt_names_the_consumed_food_and_replays_unchanged(self):
        from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
        from items.models import Item, ItemType
        from village.phase2_services import LifeActionService
        from village.models import VillageDeliveryOutbox
        Item.objects.update_or_create(item_id=29001, defaults={"item_name": "测试便携餐", "item_type": ItemType.FOOD, "is_deleted": False})
        bag, _ = NpcGlobalInventory.objects.get_or_create(npc=self.npc)
        slot = NpcGlobalInventorySlot.objects.create(bag=bag, slot_index=9001, item_id=29001, item_amount=2)
        profile = NpcProfile.objects.get(npc=self.npc)
        profile.capabilities += ["EAT"]
        profile.save()
        clock = GlobalGameTimeState.get_instance()
        clock.world_minute = 7
        clock.save(update_fields=["world_minute"])
        command = NpcCommandState.objects.create(command_id=uuid.uuid4(), message_id=uuid.uuid4(), request_id=uuid.uuid4(),
            npc=self.npc, schema_version="village.command.v1", sender_id="10011", recipient_id="env", kind="EAT",
            payload_sha256="test", status="RUNNING", started_world_minute=0, duration_minutes=7)
        NpcRuntimeState.objects.filter(npc=self.npc).update(needs_world_minute=0, active_command_id=command.pk,
            current_action="EAT", action_status="ACTING", action_ends_world_minute=7)
        payload = {"request_id": str(command.request_id), "command_id": str(command.pk), "npc_id": "10011", "action": "EAT",
            "start_world_minute": 0, "completion_world_minute": 7, "location_code": "BILL_HOME", "expected_state_version": 0,
            "params": {"source": "PERSONAL_INVENTORY", "food_instance_id": str(slot.instance_id)}}
        result = LifeActionService.execute(payload)
        self.assertTrue(result["success"], result)
        consumed = result["effects"]["items"][0]
        self.assertEqual((consumed["item_name"], consumed["amount"], consumed["food_instance_id"]), ("测试便携餐", 1, str(slot.instance_id)))
        outbox = VillageDeliveryOutbox.objects.get(command=command, message_kind="OUTCOME")
        self.assertEqual(outbox.payload["effects"]["items"][0], consumed)
        original = deepcopy(result["effects"])
        Item.objects.filter(item_id=29001).update(item_name="后来改名的便携餐")
        self.assertEqual(LifeActionService.execute(payload)["effects"], original)
        slot.refresh_from_db()
        self.assertEqual(slot.item_amount, 1)

    def test_native_social_query_returns_relationships_and_commitments(self):
        from village.models import NpcRelationship
        profile = NpcProfile.objects.get(npc=self.npc)
        profile.capabilities += ["INSPECT_SOCIAL_STATE"]
        profile.save()
        other, _ = NpcDefinition.objects.get_or_create(agent_id="10012", defaults={"name": "Jack"})
        NpcRelationship.objects.create(from_npc=self.npc, to_npc=other)
        result = accept_action(self.message("social-001", "INSPECT_SOCIAL_STATE", {}))
        self.assertTrue(result[0], result[1])
        data = task_context(result[2])["result"]["data"]
        self.assertEqual(data["relationships"][0]["other_npc_id"], "10012")
        self.assertIn("commitments", data)
        self.assertNotIn("authorized", data)
