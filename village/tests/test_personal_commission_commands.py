import copy
import time
import uuid

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory
from items.models import Item
from village.agent_queries import execute_agent_query
from village.command_catalog import COMMAND_CATALOG, agent_tool
from village.models import (NpcProfile, NpcRuntimeState, VillageActionReceipt, VillageLocationDefinition,
                            VillagePersonalCommission, VillageSimulationConfig)
from village.personal_commission_commands import MUTATIONS, QUERY
from village.phase2_services import TickService
from village.player_actions import execute_player_action
from village.runtime_services import CommandService
from village import views

RETIRED_NPC_ACTIONS = {"ACCEPT_PERSONAL_COMMISSION", "ACT_PERSONAL_COMMISSION", "ABANDON_PERSONAL_COMMISSION"}


class PersonalCommissionCommandTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults={
            "village_needs_enabled": True, "village_economy_enabled": True})
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 0
        self.clock.time_revision = 1
        self.clock.save()
        VillageLocationDefinition.objects.update_or_create(location_code="VILLAGE_SQUARE", defaults={
            "display_name": "广场", "center_anchor_code": "VILLAGE_SQUARE", "location_type": "PUBLIC", "is_active": True})
        self.bill = self.npc("10011", "Bill")
        self.lucy = self.npc("10014", "Lucy")
        self.player = GameAccount.objects.create(username="commission-command-player")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=100)
        Item.objects.update_or_create(pk=1002, defaults={"item_name": "木材", "item_price": 2})

    def npc(self, agent_id, name):
        npc, _ = NpcDefinition.objects.get_or_create(agent_id=agent_id, defaults={"name": name})
        NpcProfile.objects.update_or_create(npc=npc, defaults={"display_name": name, "occupation": "test",
            "work_location_code": "VILLAGE_SQUARE", "home_location_code": "VILLAGE_SQUARE",
            "social_location_code": "VILLAGE_SQUARE", "capabilities": sorted(MUTATIONS | {QUERY})})
        NpcGlobalInventory.objects.update_or_create(npc=npc, defaults={"money": 1000})
        NpcRuntimeState.objects.update_or_create(npc=npc, defaults={"current_location_code": "VILLAGE_SQUARE",
            "action_status": "IDLE", "health_fp": 1000, "energy_fp": 1000, "hunger_fp": 0})
        return npc

    def envelope(self, action, params):
        return dict(envelope_id=str(uuid.uuid4()), sender=self.lucy.agent_id, recipient="WuxiVillage",
            timestamp_ms=int(time.time() * 1000), content={"schema_version": "village.command.v1",
                "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "name": action, "params": params,
                "queue_mode": "STRICT", "ttl_ms": 120000, "not_before_world_minute": 0, "expires_world_minute": 10000})

    def command(self, npc, action, **params):
        envelope = self.envelope(action, params)
        envelope["sender"] = npc.agent_id
        return CommandService.accept_command(**envelope)

    def publish(self, **extra):
        params = dict(title="准备柴火", reward_gold=80, valid_for_minutes=1000,
            stages=[{"id": "first", "conditions": [{"id": "wood", "type": "item", "item_ids": [1002], "target": 2}]}])
        params.update(extra)
        ok, code, command = self.command(self.lucy, "PUBLISH_PERSONAL_COMMISSION", **params)
        self.assertTrue(ok, code)
        self.assertEqual(command.status, "SUCCEEDED")
        return VillagePersonalCommission.objects.get(pk=VillageActionReceipt.objects.get(command=command).response_data["commission_id"])

    def player_action(self, action, row, **params):
        return execute_player_action(dict(player_id=str(self.player.pk), request_id=str(uuid.uuid4()), action=action,
            params=dict(commission_id=str(row.pk), **params), current_location_code="VILLAGE_SQUARE"))

    def test_publication_receipt_replay_is_terminal_and_does_not_reserve_twice(self):
        envelope = self.envelope("PUBLISH_PERSONAL_COMMISSION", dict(title="交木材", reward_gold=80, valid_for_minutes=1000,
            stages=[{"conditions": [{"type": "item", "item_ids": [1002], "target": 2}]}]))
        result = CommandService.accept_command(**envelope)
        self.assertTrue(result[0], result)
        replay = CommandService.accept_command(**envelope)
        self.assertEqual(replay[1], "COMMAND_REPLAYED")
        self.assertEqual(result[2].pk, replay[2].pk)
        self.assertEqual(NpcGlobalInventory.objects.get(npc=self.lucy).money, 920)
        self.assertEqual(VillagePersonalCommission.objects.count(), 1)
        self.assertEqual(result[2].status, "SUCCEEDED")

    def test_publish_cannot_spoof_another_npc_identity(self):
        ok, code, _ = self.command(self.lucy, "PUBLISH_PERSONAL_COMMISSION", publisher_npc_id=self.bill.pk)
        self.assertFalse(ok)
        self.assertEqual(code, "INVALID_COMMAND_SCHEMA")
        self.assertFalse(VillagePersonalCommission.objects.exists())

    def test_retired_commands_rejected_even_with_stale_npc_capabilities(self):
        profile = self.bill.village_profile
        profile.capabilities += sorted(RETIRED_NPC_ACTIONS)
        profile.save()
        for fast in (False, True):
            with self.settings(VILLAGE_FAST_COMMAND_INTEGRATION=fast):
                for action in RETIRED_NPC_ACTIONS:
                    with self.subTest(fast=fast, action=action):
                        result = self.command(self.bill, action, commission_id=str(uuid.uuid4()), condition_id="wood", op="labor")
                        self.assertEqual(result[:2], (False, "UNKNOWN_ACTION"))
        self.assertTrue(RETIRED_NPC_ACTIONS.isdisjoint(COMMAND_CATALOG))
        self.assertTrue(RETIRED_NPC_ACTIONS.isdisjoint(CommandService.allowed_actions_for(self.bill)))
        self.assertEqual(MUTATIONS, {"PUBLISH_PERSONAL_COMMISSION", "WITHDRAW_PERSONAL_COMMISSION"})

    def test_inspect_defaults_to_published_and_all_is_read_only(self):
        row = self.publish()
        ok, code, data = execute_agent_query(self.lucy.agent_id, QUERY)
        self.assertTrue(ok, code)
        self.assertEqual(data["scope"], "published")
        self.assertEqual(data["commissions"][0]["commission_id"], str(row.pk))
        self.assertEqual(execute_agent_query(self.bill.agent_id, QUERY)[2]["commissions"], [])
        all_rows = execute_agent_query(self.bill.agent_id, QUERY, {"scope": "all"})[2]["commissions"]
        self.assertEqual(len(all_rows), 1)
        self.assertFalse(all_rows[0]["can_accept"])
        self.assertFalse(all_rows[0]["can_act"])
        self.assertIn("read", agent_tool(QUERY)["tags"])
        for scope in ("available", "accepted", [], None):
            self.assertEqual(execute_agent_query(self.bill.agent_id, QUERY, {"scope": scope})[1], "INVALID_QUERY_PARAMS")

    def test_query_remains_capability_gated(self):
        profile = self.bill.village_profile
        profile.capabilities = []
        profile.save()
        self.assertEqual(execute_agent_query(self.bill.agent_id, QUERY)[1], "ACTION_NOT_ALLOWED")

    def test_only_publisher_can_withdraw_and_player_acceptance_locks_it(self):
        row = self.publish()
        self.assertFalse(self.command(self.bill, "WITHDRAW_PERSONAL_COMMISSION", commission_id=str(row.pk))[0])
        self.assertTrue(self.command(self.lucy, "WITHDRAW_PERSONAL_COMMISSION", commission_id=str(row.pk))[0])
        row.refresh_from_db()
        self.assertEqual(row.status, "WITHDRAWN")
        self.assertEqual(NpcGlobalInventory.objects.get(npc=self.lucy).money, 1000)
        row = self.publish()
        self.assertTrue(self.player_action("commission_accept", row)["success"])
        self.assertFalse(self.command(self.lucy, "WITHDRAW_PERSONAL_COMMISSION", commission_id=str(row.pk))[0])
        row.refresh_from_db()
        self.assertEqual(row.status, "ACTIVE")

    def test_player_can_complete_npc_published_commission_and_replay_once(self):
        row = self.publish()
        self.assertTrue(self.player_action("commission_accept", row)["success"])
        InventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=1002, item_amount=2)
        payload = dict(player_id=str(self.player.pk), request_id=str(uuid.uuid4()), action="commission_contribute",
            params=dict(commission_id=str(row.pk), condition_id="wood", item_id=1002, quantity=2),
            current_location_code="VILLAGE_SQUARE")
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        self.assertEqual(first, execute_player_action(copy.deepcopy(payload)))
        row.refresh_from_db(); self.bag.refresh_from_db()
        self.assertEqual((row.status, self.bag.money), ("COMPLETED", 180))
        self.assertEqual(NpcGlobalInventory.objects.get(npc=self.lucy).slots.get(item_id=1002).item_amount, 2)

    def test_authority_time_expires_and_refunds_open_commission(self):
        row = self.publish(valid_for_minutes=5)
        TickService.advance({"request_id": str(uuid.uuid4()), "world_minute": 5, "time_revision": 7})
        row.refresh_from_db()
        self.assertEqual(row.status, "EXPIRED")
        self.assertEqual(NpcGlobalInventory.objects.get(npc=self.lucy).money, 1000)

    def test_accept_endpoint_returns_durable_withdraw_failure_without_second_execution(self):
        row = self.publish()
        self.assertTrue(self.player_action("commission_accept", row)["success"])
        envelope = self.envelope("WITHDRAW_PERSONAL_COMMISSION", {"commission_id": str(row.pk)})
        request = APIRequestFactory().post("/", {**envelope, "timestamp": envelope["timestamp_ms"]}, format="json")
        force_authenticate(request, user=type("Env", (), {"is_authenticated": True})())
        response = views.commands_accept(request)
        self.assertEqual(response.status_code, 409)
        data = response.data["data"]
        self.assertEqual(data["status"], "FAILED")
        self.assertFalse(data["receipt"]["success"])
        self.assertEqual(data["action"], "WITHDRAW_PERSONAL_COMMISSION")
        self.assertIsNotNone(data["state_version"])
