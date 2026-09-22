import time
import uuid
from unittest.mock import patch

from django.http import QueryDict
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType
from marketplace.services import buy_listing_for_npc, create_npc_listing
from village.agent_queries import QUERY_NAMES, execute_agent_query
from village.command_catalog import COMMAND_CATALOG, agent_tool
from village.phase3_contract import PLOT_CODES
from village.dialogue_service import DialogueService
from village.models import (
    DailyPriceSnapshot, NpcProfile, NpcRuntimeState, NpcStoryFactAccess, VillageLocationDefinition,
    VillageStoryArc, VillageStoryFact,
    VillageActionDurationConfig,
    VillageFarmPlot,
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
        Item.objects.create(
            item_id=1030, item_name="玉米", item_type=ItemType.FOOD, item_price=8
        )
        Item.objects.create(
            item_id=1049, item_name="盐", item_type=ItemType.SEASONING, item_price=2
        )
        Item.objects.create(
            item_id=1051, item_name="玉米种子", item_type=ItemType.SEED, item_price=5
        )
        self.food = NpcGlobalInventorySlot.objects.create(
            bag=self.inventory, slot_index=0, item_id=1030, item_amount=3,
        )
        self.corn_seed = NpcGlobalInventorySlot.objects.create(
            bag=self.inventory, slot_index=1, item_id=1051, item_amount=2,
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
        self.assertEqual(data["items"][0]["instance_id"], str(self.food.instance_id))
        self.assertEqual(
            data["items"][0]["sale"],
            {"target": "GENERAL_STORE", "unit_price": 8},
        )
        self.assertEqual(
            set(data["items"][0]),
            {"food_instance_id", "instance_id", "item_id", "name", "item_type", "quantity", "available_quantity", "sale"},
        )
        self.assertNotIn(str(self.other_food.instance_id), str(data))
        seed = next(row for row in data["items"] if row["item_id"] == 1051)
        self.assertEqual(
            set(seed),
            {"instance_id", "item_id", "name", "item_type", "quantity", "available_quantity", "sale", "planting"},
        )
        self.assertEqual(seed["sale"], {"target": "GENERAL_STORE", "unit_price": 5})
        self.assertEqual(seed["instance_id"], str(self.corn_seed.instance_id))
        self.assertEqual(
            seed["planting"],
            {
                "crop_code": "CORN",
                "harvest_item_id": 1030,
                "harvest_item_name": "玉米",
                "harvest_quantity_min": 4,
                "harvest_quantity_max": 6,
                "growth_minutes": 1440,
            },
        )

    @patch("village.caravan.agent_projection")
    def test_market_state_filters_and_pages_caravan_goods(self, projection):
        projection.side_effect = lambda minute: {
            "caravan": {"status": "PRESENT"},
            "items": [{"item_id": 1005, "export_unit_price": 4},
                      {"item_id": 1049, "import_available_amount": 3, "import_unit_price": 7}],
        }
        success, code, data = execute_agent_query("10011", "INSPECT_MARKET_STATE")
        self.assertTrue(success, code)
        success, code, salt = execute_agent_query("10011", "INSPECT_MARKET_STATE", {"item_id": "1049"})
        self.assertTrue(success, code)
        self.assertEqual(salt["total"], 1)
        self.assertEqual(salt["items"][0]["import_unit_price"], 7)
        self.assertNotIn("requestable_items", salt)
        self.assertNotIn("delivery_points", data)
        invalid, invalid_code, _ = execute_agent_query(
            "10011", "INSPECT_MARKET_STATE", {"page_size": "51"}
        )
        self.assertFalse(invalid)
        self.assertEqual(invalid_code, "INVALID_QUERY_PARAMS")
        with self.subTest("strict query integers and missing item"):
            for params in ({"page_size": True}, {"page_size": "1.5"}, {"page": "0"}, {"page": "x"}):
                ok, result_code, _ = execute_agent_query("10011", "INSPECT_MARKET_STATE", params)
                self.assertFalse(ok)
                self.assertEqual(result_code, "INVALID_QUERY_PARAMS")
            first_ok, _, first = execute_agent_query("10011", "INSPECT_MARKET_STATE", {"page": "1", "page_size": "1"})
            second_ok, _, second = execute_agent_query("10011", "INSPECT_MARKET_STATE", {"page": "2", "page_size": "1"})
            self.assertTrue(first_ok and second_ok)
            self.assertNotEqual(first["items"][0]["item_id"], second["items"][0]["item_id"])
            missing_ok, _, missing = execute_agent_query("10011", "INSPECT_MARKET_STATE", {"item_id": "9999999"})
            self.assertTrue(missing_ok)
            self.assertFalse(missing["item_found"])

    def test_farm_state_accepts_single_plot_and_rejects_invalid_plot_code(self):
        for code in PLOT_CODES:
            VillageFarmPlot.objects.get_or_create(plot_code=code)

        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 1000
        time_state.save(update_fields=["world_minute"])
        VillageFarmPlot.objects.filter(plot_code="FARM_PLOT_A_02").update(
            status="GROWING", crop_code="CORN",
            planted_world_minute=280, mature_world_minute=1720,
        )
        VillageFarmPlot.objects.filter(plot_code="FARM_PLOT_A_03").update(
            status="MATURE", crop_code="CARROT",
            planted_world_minute=-440, mature_world_minute=1000,
        )

        success, code, all_data = execute_agent_query("10011", "INSPECT_FARM_STATE")
        self.assertTrue(success, code)
        self.assertEqual(len(all_data["plots"]), 36)
        self.assertEqual(
            all_data["plots"][0],
            {"plot_code": "FARM_PLOT_A", "status": "EMPTY"},
        )
        self.assertEqual(
            all_data["plots"][1],
            {
                "plot_code": "FARM_PLOT_A_02",
                "status": "GROWING",
                "crop_code": "CORN",
                "crop_name": "玉米",
                "remaining_minutes": 720,
            },
        )
        self.assertEqual(
            all_data["plots"][2],
            {
                "plot_code": "FARM_PLOT_A_03",
                "status": "MATURE",
                "crop_code": "CARROT",
                "crop_name": "胡萝卜",
                "harvestable": True,
            },
        )
        self.assertEqual(set(all_data), {"plots"})
        success, code, single_data = execute_agent_query(
            "10011", "INSPECT_FARM_STATE", QueryDict("plot_code= farm_plot_a ")
        )
        self.assertTrue(success, code)
        self.assertEqual(
            single_data,
            {"plots": [{"plot_code": "FARM_PLOT_A", "status": "EMPTY"}]},
        )
        for params in ({"plot_code": ""}, {"plot_code": None}, {"plot_code": 1}, {"plot_code": "NOPE"}):
            success, code, _ = execute_agent_query("10011", "INSPECT_FARM_STATE", params)
            self.assertFalse(success)
            self.assertEqual(code, "INVALID_QUERY_PARAMS")
        self.assertEqual(
            COMMAND_CATALOG["INSPECT_FARM_STATE"]["parameters"]["properties"]["plot_code"]["type"],
            "string",
        )

    def test_public_economy_reports_missing_draft_and_published_states(self):
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 0
        time_state.save(update_fields=["world_minute"])
        for expected, draft, published in (("MISSING", False, False), ("DRAFT", True, False), ("PUBLISHED", False, True)):
            DailyPriceSnapshot.objects.all().delete()
            if expected != "MISSING":
                DailyPriceSnapshot.objects.create(day_index=0, status=expected, prices={})
            success, code, data = execute_agent_query("10011", "INSPECT_PUBLIC_ECONOMY_STATE")
            self.assertTrue(success, code)
            self.assertEqual(data["price_index_status"], expected)
            self.assertEqual(data["draft_exists"], draft)
            self.assertEqual(data["published"], published)

    def test_capability_gate_and_nearby_projection(self):
        success, code, data = execute_agent_query("10011", "INSPECT_NEARBY")
        self.assertTrue(success)
        self.assertEqual(data["nearby_npcs"][0]["npc_id"], "90012")
        self.assertEqual(data["nearby_players"], [])
        self.assertFalse(data["players_observation_available"])
        self.assertEqual(data["npc_names"]["90012"], "Jack")
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
        self.assertNotIn("needs_rules", data)
        self.assertEqual(data["self_npc_id"], "10011")
        self.assertEqual(data["self_name"], "Bill")

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

    def test_resource_affairs_remain_visible_after_legacy_story_end_and_meetings(self):
        from village.models import VillageAffair
        profile = NpcProfile.objects.get(npc=self.bill)
        profile.capabilities = self.capabilities + ["STORY_ACT"]
        profile.save(update_fields=["capabilities"])
        arc = VillageStoryArc.objects.create(arc_code="BROKEN_TRADE_ROUTE_V1", current_phase="CRISIS_CHECK",
                                            current_plan="RATION_AND_WAIT", is_completed=True, outcome="LONG_SHORTAGE")
        VillageStoryFact.objects.create(arc=arc)
        affair = VillageAffair.objects.create(code="BRIDGE_REPAIR", title="修桥", opened_world_minute=0,
            locations=["REPAIR_YARD"], requirements={"materials": {"1002": 100}, "meals": 20, "gold": 500, "labor": 20},
            progress={"materials": {}, "meals": 0, "gold": 0, "labor": 0})
        success, code, data = execute_agent_query("10011", "INSPECT_STORY_STATE")
        self.assertTrue(success, code)
        self.assertEqual(data["status"], "ACTIVE")
        self.assertIn("DONATE_REPAIR_RESOURCES", data["available_actions"])
        self.assertNotIn("REPAIR_LABOR", data["available_actions"])
        self.assertEqual(data["affairs"][0]["gold"]["remaining"], 500)
        affair.progress = {"materials": {"1002": 100}, "meals": 20, "gold": 500, "labor": 0}
        affair.status = "READY"
        affair.save()
        success, code, data = execute_agent_query("10011", "INSPECT_STORY_STATE")
        self.assertTrue(success, code)
        self.assertIn("REPAIR_LABOR", data["available_actions"])
        self.assertNotIn("DONATE_REPAIR_RESOURCES", data["available_actions"])

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
            self.assertEqual(set(tool), {"name", "description", "parameters", "tags"})

    def test_movement_commands_advertise_deferred_execution_tags(self):
        self.assertIn("long_running", agent_tool("MOVE")["tags"])
        self.assertIn("movement", agent_tool("MOVE")["tags"])
        self.assertIn("long_running", agent_tool("GO_HOME")["tags"])
        self.assertIn("movement", agent_tool("GO_HOME")["tags"])

    def test_context_contains_only_rules_and_follows_capabilities_and_configuration(self):
        user = GameAccount.objects.create(username="context-env", password="unused")
        request = APIRequestFactory().get("/api/village/npcs/10011/discover")
        force_authenticate(request, user=user)
        profile = NpcProfile.objects.get(npc=self.bill)
        profile.capabilities = ["OBSERVE", "PLANT", "SOCIALIZE"]
        profile.save(update_fields=["capabilities"])
        VillageActionDurationConfig.objects.update_or_create(
            action_name="PLANT", defaults={"duration_minutes": 7}
        )

        data = npc_available_actions(request, "10011").data["data"]
        context = data["agent_context"]
        self.assertEqual(set(context), {"common_rules", "npc_rules"})
        self.assertIn("对话是环境规则", "\n".join(context["common_rules"]))
        self.assertNotIn("对话是环境规则", "\n".join(context["npc_rules"]))
        text = "\n".join(context["common_rules"] + context["npc_rules"])
        self.assertIn("7世界分钟", text)
        self.assertIn("INSPECT_FARM_STATE", text)
        self.assertIn("INSPECT_INVENTORY", text)
        self.assertIn("path_distance", text)
        self.assertIn("reachable=true", text)
        self.assertIn("每完成一块后重新调用 INSPECT_FARM_STATE", text)
        self.assertIn("不能沿用移动前的距离顺序", text)
        self.assertNotIn("CORN(", text)
        self.assertNotIn("玉米", text)
        self.assertNotIn("HUNT", text)
        self.assertNotIn("SOCIALIZE", text)
        self.assertIn("10011", text)
        # 地点代码是导航所需的导航上下文（与 discover 的 npc_profile/locations 一致），
        # 必须出现在规则文本中，否则 Agent 无法得知工作地点（如农田 BILL_FARM）。
        self.assertIn("BILL_HOME", text)
        self.assertIn("MARKET_STREET", text)
        self.assertIn("BILL_FARM", text)
        # 村庄地点目录与 discover 可见性一致：公开地点可见，剧情门槛地点不可见。
        self.assertIn("BILL_HOME=Bill农舍", text)
        self.assertIn("MARKET_STREET=市场街", text)
        self.assertNotIn("SECRET", text)
        self.assertEqual({tool["name"] for tool in data["tools"]}, {"GET_RULE", "OBSERVE", "PLANT"})

        NpcRuntimeState.objects.filter(npc=self.bill).update(
            action_status="RUNNING", hunger_fp=990, current_location_code="MARKET_STREET"
        )
        self.assertEqual(npc_available_actions(request, "10011").data["data"]["agent_context"], context)

        profile.capabilities = ["OBSERVE", "HUNT"]
        profile.save(update_fields=["capabilities"])
        data = npc_available_actions(request, "10011").data["data"]
        text = "\n".join(data["agent_context"]["npc_rules"])
        self.assertIn("对话是环境规则", "\n".join(data["agent_context"]["common_rules"]))
        self.assertNotIn("对话是环境规则", text)
        self.assertIn("HUNT", text)
        self.assertNotIn("玉米", text)
        self.assertNotIn("PLANT", text)
        self.assertEqual({tool["name"] for tool in data["tools"]}, {"GET_RULE", "OBSERVE", "HUNT"})

        with patch.dict(CommandService.NEED_GATE_LIMITS, {"hunger": 800}):
            data = npc_available_actions(request, "10011").data["data"]
            self.assertIn("饥饿必须低于80", "\n".join(data["agent_context"]["npc_rules"]))
            runtime = NpcRuntimeState.objects.get(npc=self.bill)
            runtime.hunger_fp = 800
            self.assertEqual(CommandService.unavailable_reason("HUNT", runtime), ResultCode.HUNGER_TOO_HIGH)

    def test_get_rule_returns_per_npc_environment_rules(self):
        user = GameAccount.objects.create(username="get-rule-env", password="unused")
        request = APIRequestFactory().get("/api/village/npcs/10011/discover")
        force_authenticate(request, user=user)
        profile = NpcProfile.objects.get(npc=self.bill)
        profile.capabilities = ["GET_RULE", "OBSERVE", "PLANT", "SOCIALIZE"]
        profile.save(update_fields=["capabilities"])
        VillageActionDurationConfig.objects.update_or_create(
            action_name="PLANT", defaults={"duration_minutes": 7}
        )

        discover_context = npc_available_actions(request, "10011").data["data"]["agent_context"]
        success, code, data = execute_agent_query("10011", "GET_RULE")
        self.assertTrue(success)
        self.assertEqual(code, "OK")
        self.assertEqual(discover_context, data)
        self.assertEqual(set(data), {"common_rules", "npc_rules"})
        text = "\n".join(data["common_rules"] + data["npc_rules"])
        self.assertIn("7世界分钟", text)
        self.assertIn("INSPECT_FARM_STATE", text)
        self.assertIn("INSPECT_INVENTORY", text)
        self.assertNotIn("CORN(", text)
        self.assertNotIn("玉米", text)
        self.assertIn("BILL_FARM", text)
        self.assertNotIn("HUNT", text)
        self.assertIn("10011", text)
        self.assertIn("BILL_HOME=Bill农舍", text)
        self.assertNotIn("SECRET", text)
        # 二跑复盘 R4：GET_RULE 必须写明对话距离规则（4 米门槛、INSPECT_NEARBY 用法、失败只追一次）。
        self.assertIn("4米以内", text)
        self.assertIn("INSPECT_NEARBY", text)
        self.assertIn("MOVE 一次并重试一次", text)
        self.assertIn(f"单次发言不能为空且不能超过{DialogueService.MAX_TEXT_LENGTH}个字符", text)
        self.assertIn("该句话不会显示或发送", text)

        profile.capabilities = ["GET_RULE", "OBSERVE", "HUNT"]
        profile.save(update_fields=["capabilities"])
        success, code, data = execute_agent_query("10011", "GET_RULE")
        self.assertTrue(success)
        text = "\n".join(data["npc_rules"])
        self.assertIn("HUNT", text)
        self.assertNotIn("玉米", text)
        self.assertNotIn("PLANT", text)

    def test_get_rule_denied_without_capability(self):
        profile = NpcProfile.objects.get(npc__agent_id="90012")
        self.assertEqual(profile.capabilities, [])
        success, code, data = execute_agent_query("90012", "GET_RULE")
        self.assertFalse(success)
        self.assertEqual(code, ResultCode.ACTION_NOT_ALLOWED)

    def test_dialogue_capability_repair_preserves_other_permissions(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        from village.seed_data import BASE_ACTIONS

        profile = NpcProfile.objects.get(npc=self.bill)
        profile.capabilities = ["PLANT", "SOCIALIZE"]
        profile.save(update_fields=["capabilities"])
        migrate = import_module("village.migrations.0028_restore_dialogue_capabilities").restore_dialogue_capabilities
        migrate(apps, connection.schema_editor())
        migrate(apps, connection.schema_editor())
        profile.refresh_from_db()
        self.assertEqual(profile.capabilities, ["PLANT", "START_DIALOGUE", "SPEAK"])
        self.assertEqual(NpcProfile.objects.get(npc__agent_id="90012").capabilities, [])
        # Historical migration stays immutable; current seeds no longer grant its commands.
        self.assertTrue({"START_DIALOGUE", "SPEAK"}.isdisjoint(BASE_ACTIONS))
        self.assertNotIn("SOCIALIZE", BASE_ACTIONS)

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
            ["HUNT_ANIMALS"],
        )
        self.assertEqual(
            set(COMMAND_CATALOG["BROWSE_MARKETPLACE"]["parameters"]["properties"]),
            {"item_id", "limit"},
        )
        for action in ("LIST_STORE_ITEM", "INSPECT_STORE_INVENTORY"):
            self.assertEqual(
                COMMAND_CATALOG[action]["parameters"]["properties"]["store_code"]["enum"],
                ["GENERAL_STORE", "RESTAURANT_STORE"],
            )
        self.assertIn("10米", COMMAND_CATALOG["INSPECT_NEARBY"]["description"])
        self.assertIn("nearby_players", COMMAND_CATALOG["INSPECT_NEARBY"]["description"])
        self.assertIn("players_observation_available", COMMAND_CATALOG["INSPECT_NEARBY"]["description"])
        self.assertTrue({"START_DIALOGUE", "SPEAK", "SPEECH"}.isdisjoint(COMMAND_CATALOG))

    def test_browse_marketplace_accepts_django_query_params(self):
        success, code, data = execute_agent_query(
            "10011", "BROWSE_MARKETPLACE", QueryDict("item_id=1030&limit=20")
        )
        self.assertTrue(success, code)
        self.assertEqual(code, "OK")
        self.assertEqual(data["page_size"], 20)

    @patch("village.agent_queries.browse_marketplace_query", return_value={"listings": [{"listing_id": "1"}]})
    def test_browse_marketplace_query_is_exposed(self, browse_marketplace_query):
        success, code, data = execute_agent_query("10011", "BROWSE_MARKETPLACE")
        self.assertTrue(success)
        self.assertEqual(code, "OK")
        self.assertEqual(data["listings"][0]["listing_id"], "1")
        browse_marketplace_query.assert_called_once()

    @patch("village.agent_queries.my_marketplace_listings_query", return_value={"listings": [{"listing_id": "2"}]})
    def test_my_marketplace_listings_query_is_exposed(self, my_marketplace_listings_query):
        success, code, data = execute_agent_query("10011", "CHECK_MY_MARKETPLACE_LISTINGS")
        self.assertTrue(success)
        self.assertEqual(code, "OK")
        self.assertEqual(data["listings"][0]["listing_id"], "2")
        my_marketplace_listings_query.assert_called_once()

    @patch("village.agent_queries.marketplace_history_query", return_value={"transactions": [{"transaction_id": "3"}]})
    def test_marketplace_history_query_is_exposed(self, marketplace_history_query):
        success, code, data = execute_agent_query("10011", "CHECK_MARKETPLACE_HISTORY")
        self.assertTrue(success)
        self.assertEqual(code, "OK")
        self.assertEqual(data["transactions"][0]["transaction_id"], "3")
        marketplace_history_query.assert_called_once()

    def test_marketplace_queries_serialize_real_service_results(self):
        listing, err = create_npc_listing(
            self.bill,
            self.food.instance_id,
            2,
            15,
            expected_asset_version=self.inventory.asset_version,
        )
        self.assertIsNone(err)
        buy_result, err = buy_listing_for_npc(other := NpcDefinition.objects.get(agent_id="90012"), listing.pk, 1)
        self.assertIsNone(err)
        self.assertIsNotNone(buy_result["transaction"])

        success, code, data = execute_agent_query("10011", "BROWSE_MARKETPLACE")
        self.assertTrue(success, code)
        self.assertEqual(data["listings"], [])  # Buyers cannot buy their own listings.
        NpcProfile.objects.filter(npc=other).update(capabilities=["BROWSE_MARKETPLACE"])
        success, code, data = execute_agent_query("90012", "BROWSE_MARKETPLACE")
        self.assertTrue(success, code)
        self.assertEqual(data["listings"][0]["listingId"], listing.pk)
        self.assertIn("sellerName", data["listings"][0])

        success, code, data = execute_agent_query("10011", "CHECK_MY_MARKETPLACE_LISTINGS")
        self.assertTrue(success, code)
        self.assertEqual(data["listings"][0]["listingId"], listing.pk)
        self.assertIn("remainingAmount", data["listings"][0])

        success, code, data = execute_agent_query("10011", "CHECK_MARKETPLACE_HISTORY")
        self.assertTrue(success, code)
        self.assertEqual(data["transactions"][0]["listingId"], listing.pk)
        self.assertEqual(data["transactions"][0]["buyerName"], "jack")


    def test_discover_exposes_complete_eat_and_commitment_schemas(self):
        profile = NpcProfile.objects.get(npc=self.bill)
        profile.capabilities = ["EAT", "CREATE_COMMITMENT", "PROPOSE_TRADE"]
        profile.save(update_fields=["capabilities"])
        request = APIRequestFactory().get("/api/village/npcs/10011/discover")
        force_authenticate(request, user=GameAccount.objects.create(username="schema-env", password="unused"))
        response = npc_available_actions(request, "10011")
        tools = {t["name"]: t for t in response.data["data"]["tools"]}
        eat = tools["EAT"]["parameters"]
        self.assertEqual(set(eat["required"]), {"source", "food_instance_id"})
        self.assertEqual(eat["properties"]["source"]["enum"], ["PERSONAL_INVENTORY"])
        self.assertEqual(eat["properties"]["food_instance_id"]["format"], "uuid")
        commitments = tools["CREATE_COMMITMENT"]["parameters"]
        self.assertEqual(len(commitments["oneOf"]), 6)
        self.assertEqual(len(commitments["examples"]), 6)
        self.assertIn("commitment_id", tools["PROPOSE_TRADE"]["parameters"]["properties"])
