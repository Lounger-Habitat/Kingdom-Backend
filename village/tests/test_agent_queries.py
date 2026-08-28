import time
import uuid
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType
from village.agent_queries import QUERY_NAMES, execute_agent_query
from village.command_catalog import COMMAND_CATALOG
from village.models import (
    NpcProfile, NpcRuntimeState, NpcStoryFactAccess, VillageLocationDefinition,
    VillageStoryArc, VillageStoryFact,
)
from village.runtime_services import CommandService, ResultCode
from village.views import npc_available_actions


class AgentQueryTests(TestCase):
    def setUp(self):
        self.capabilities = sorted(QUERY_NAMES)
        self.home = VillageLocationDefinition.objects.create(
            location_code="BILL_HOME", display_name="Bill农舍", center_anchor_code="BILL_HOME",
            location_type="home", bounds={"center": [0, 0, 0]}, is_public=False,
        )
        VillageLocationDefinition.objects.create(
            location_code="MARKET_STREET", display_name="市场街", center_anchor_code="MARKET_STREET",
            location_type="market", bounds={"center": [2, 0, 0]}, is_public=True,
        )
        VillageLocationDefinition.objects.create(
            location_code="SECRET", display_name="隐藏地点", center_anchor_code="SECRET",
            location_type="story", bounds={"center": [1, 0, 0]}, is_public=True,
            required_story_states=["UNLOCKED"],
        )
        self.bill = NpcDefinition.objects.create(agent_id="10011", name="bill")
        NpcProfile.objects.create(
            npc=self.bill, display_name="Bill", occupation="farmer",
            home_location_code="BILL_HOME", work_location_code="MARKET_STREET",
            social_location_code="MARKET_STREET", capabilities=self.capabilities,
        )
        NpcRuntimeState.objects.create(npc=self.bill, current_location_code="BILL_HOME")
        self.inventory = NpcGlobalInventory.objects.create(npc=self.bill, money=20, asset_version=9)
        Item.objects.create(item_id=1030, item_name="玉米", item_type=ItemType.FOOD)
        self.food = NpcGlobalInventorySlot.objects.create(
            bag=self.inventory, slot_index=0, item_id=1030, item_amount=3,
        )
        other = NpcDefinition.objects.create(agent_id="90012", name="jack")
        NpcProfile.objects.create(
            npc=other, display_name="Jack", occupation="hunter",
            home_location_code="BILL_HOME", work_location_code="MARKET_STREET",
            social_location_code="MARKET_STREET", capabilities=[],
        )
        NpcRuntimeState.objects.create(npc=other, current_location_code="BILL_HOME")
        other_inventory = NpcGlobalInventory.objects.create(npc=other, money=999)
        self.other_food = NpcGlobalInventorySlot.objects.create(
            bag=other_inventory, slot_index=0, item_id=1056, item_amount=8,
        )
        GlobalGameTimeState.get_instance()

    def test_inventory_is_private_and_supplies_eat_parameter_without_versions(self):
        success, code, data = execute_agent_query("10011", "INSPECT_INVENTORY")
        self.assertTrue(success)
        self.assertEqual(code, "OK")
        self.assertNotIn("asset_version", data)
        self.assertEqual(data["items"][0]["food_instance_id"], str(self.food.instance_id))
        self.assertNotIn(str(self.other_food.instance_id), str(data))

    def test_capability_gate_and_nearby_projection(self):
        success, code, data = execute_agent_query("10011", "INSPECT_NEARBY")
        self.assertTrue(success)
        self.assertEqual(data["nearby_npcs"][0]["npc_id"], "90012")
        self.assertEqual([row["location_code"] for row in data["nearby_locations"]], ["MARKET_STREET"])
        success, code, data = execute_agent_query("90012", "INSPECT_INVENTORY")
        self.assertFalse(success)
        self.assertEqual(code, "ACTION_NOT_ALLOWED")
        self.assertIsNone(data)

    def test_observe_reports_running_sleep_and_remaining_minutes(self):
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 100
        time_state.save(update_fields=["world_minute"])
        runtime = NpcRuntimeState.objects.get(npc=self.bill)
        command_id = uuid.uuid4()
        runtime.current_action = "SLEEP"
        runtime.action_status = "SLEEPING"
        runtime.action_started_world_minute = 40
        runtime.action_ends_world_minute = 520
        runtime.active_command_id = command_id
        runtime.save()

        success, code, data = execute_agent_query("10011", "OBSERVE")

        self.assertTrue(success, code)
        self.assertEqual(data["remaining_minutes"], 420)
        self.assertEqual(data["active_command_id"], str(command_id))
        self.assertTrue(data["interruptible"])
        self.assertIn("need_bands", data)
        self.assertEqual(data["needs_rules"]["eat_completion_effect"], {"hunger": -45, "energy": 5})

    def test_story_directory_and_npc_scoped_detail(self):
        success, code, data = execute_agent_query("10011", "INSPECT_RELEVANT_STORIES")
        self.assertTrue(success, code)
        self.assertEqual(data["stories"], [])

        arc = VillageStoryArc.objects.create(
            arc_code="BROKEN_TRADE_ROUTE_V1", current_phase="EXECUTION",
            current_plan="BRIDGE_REPAIR", story_version=4,
        )
        VillageStoryFact.objects.create(arc=arc, bridge_condition="REPAIRING", repair_wood=3)
        NpcStoryFactAccess.objects.create(
            npc=self.bill, fact_code="BRIDGE_FOUNDATION_WARNING", unlocked_world_minute=0,
        )
        other = NpcDefinition.objects.get(agent_id="90012")
        NpcStoryFactAccess.objects.create(
            npc=other, fact_code="FOREST_OLD_ROUTE", unlocked_world_minute=0,
        )

        success, code, data = execute_agent_query(
            "10011", "INSPECT_STORY_DETAIL", {"story_id": "BROKEN_TRADE_ROUTE_V1"}
        )
        self.assertTrue(success, code)
        self.assertEqual(data["bridge_condition"], "REPAIRING")
        self.assertEqual(data["progress"]["repair_wood"], 3)
        self.assertEqual(data["known_facts"], ["BRIDGE_FOUNDATION_WARNING"])
        success, code, legacy = execute_agent_query("10011", "INSPECT_STORY_STATE")
        self.assertTrue(success, code)
        self.assertEqual(legacy["bridge_condition"], "REPAIRING")
        success, code, data = execute_agent_query(
            "10011", "INSPECT_STORY_DETAIL", {"story_id": "NO_SUCH_STORY"}
        )
        self.assertFalse(success)
        self.assertEqual(code, "STORY_NOT_VISIBLE")

    def test_discover_has_low_frequency_profile_and_visible_locations_only(self):
        user = GameAccount.objects.create(username="agent-query-env", password="unused")
        request = APIRequestFactory().get("/api/village/npcs/10011/discover")
        force_authenticate(request, user=user)
        response = npc_available_actions(request, "10011")
        data = response.data["data"]
        self.assertEqual(data["npc_profile"]["home_location"], "BILL_HOME")
        self.assertEqual({row["location_code"] for row in data["locations"]}, {"BILL_HOME", "MARKET_STREET"})
        self.assertNotIn("state_version", data["npc_profile"])
        for tool in data["tools"]:
            self.assertEqual(set(tool), {"name", "description", "parameters"})

    @patch("village.runtime_services._fast_integration_enabled", return_value=True)
    def test_all_read_queries_are_rejected_by_command_lifecycle(self, _fast):
        self.assertEqual(CommandService.READ_QUERY_ACTIONS, QUERY_NAMES)
        for query_name in QUERY_NAMES:
            ok, code, command = CommandService.accept_command(
                str(uuid.uuid4()), "10011", "env", {
                    "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()),
                    "name": query_name, "params": {},
                }, int(time.time() * 1000),
            )
            self.assertFalse(ok, query_name)
            self.assertEqual(code, ResultCode.ACTION_NOT_ALLOWED, query_name)
            self.assertIsNone(command)

    def test_catalog_contains_queries_and_hunt_enum(self):
        self.assertTrue(QUERY_NAMES.issubset(COMMAND_CATALOG))
        self.assertEqual(
            COMMAND_CATALOG["HUNT"]["parameters"]["properties"]["hunt_code"]["enum"],
            ["HUNT_CHICKEN"],
        )
