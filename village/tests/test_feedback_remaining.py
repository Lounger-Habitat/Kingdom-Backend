from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from village.goal_services import _state_data
from village import agent_queries
from village import phase3_marketplace
from datetime import timedelta
from django.utils import timezone


class RemainingFeedbackTests(SimpleTestCase):
    def test_restaurant_closed_state_and_private_data_are_scoped_to_its_owner(self):
        owner = SimpleNamespace(pk=14, agent_id="10014")
        visitor = SimpleNamespace(pk=15, agent_id="10015")
        store = SimpleNamespace(pk="RESTAURANT_STORE", location_code="RESTAURANT", status="CLOSED", operator_npc_id=14)
        with patch.object(agent_queries.VillageStore.objects, "filter") as stores, \
             patch.object(agent_queries, "_world_minute", return_value=100), \
             patch("village.phase3_direct_trade.query_store_inventory", return_value=("TRADE_STORE_CLOSED", None)), \
             patch("village.phase3_direct_trade.store_inventory_status", return_value=("NORMAL", {})), \
             patch.object(agent_queries.NpcGlobalInventory.objects, "get") as inventory, \
             patch.object(agent_queries, "_cooking_recipes", return_value=[]), \
             patch("village.caravan.store_finances", return_value={"store_code":"RESTAURANT_STORE", "balance":10}) as finances, \
             patch("village.lucy_cooking.pending_reviews", return_value={"reviews":[]}), \
             patch("village.lucy_restaurant.public_meal_availability", return_value={}):
            stores.return_value.first.return_value = store
            inventory.return_value.slots.filter.return_value = []
            data = agent_queries._restaurant_state(owner)
            self.assertEqual(data["status"], "CLOSED")
            self.assertIsNone(data["menu"])
            self.assertNotIn("recipes", data)
            finances.assert_called_once_with("10014", store_code="RESTAURANT_STORE")
            finances.reset_mock(); inventory.reset_mock()
            public = agent_queries._restaurant_state(visitor)
            self.assertEqual(public["status"], "CLOSED")
            self.assertNotIn("store_finances", public)
            self.assertNotIn("pending_reviews", public)
            self.assertNotIn("public_meal_availability", public)
            finances.assert_not_called(); inventory.assert_not_called()

    def test_recipe_catalog_is_available_through_existing_rules_only_for_cooks(self):
        npc = SimpleNamespace(village_profile=object())
        recipe = {"recipe_id":3, "ingredients":[{"item_id":1057,"required_quantity":2}]}
        with patch("village.runtime_services.CommandService.allowed_actions_for", return_value={"COOK"}) as actions, \
             patch("village.agent_context.build_agent_context", side_effect=lambda *a,**k:{}), \
             patch.object(agent_queries, "_cooking_recipes", return_value=[recipe]):
            self.assertEqual(agent_queries._rules(npc)["cooking_recipes"], [recipe])
            actions.return_value = {"GET_RULE"}
            self.assertNotIn("cooking_recipes", agent_queries._rules(npc))

    def test_browse_filters_before_pagination_without_settlement(self):
        now = timezone.now()
        npc = SimpleNamespace(pk=11, global_inventory=SimpleNamespace(money=10))
        def listing(pk, **changes):
            values = dict(pk=pk, status="active", remaining_amount=5, seller_npc_id=12,
                          price=3, expires_at=None, snapshot={"freshnessUnknown": False})
            values.update(changes)
            return SimpleNamespace(**values)
        rows = [listing(1, seller_npc_id=11), listing(2, status="sold_out"),
                listing(3, remaining_amount=0), listing(4, expires_at=now-timedelta(seconds=1)),
                listing(5, snapshot={"freshnessUnknown": True}),
                listing(6, snapshot={"expiresWorldMinute": 100}), listing(7, price=11),
                listing(8), listing(9, price=0)]
        with patch("game_time.models.GlobalGameTimeState.objects.get", return_value=SimpleNamespace(world_minute=100)), \
             patch.object(phase3_marketplace.marketplace_services, "_listing_batch", side_effect=lambda r:r.snapshot), \
             patch.object(phase3_marketplace, "serialize_listing", side_effect=lambda r,n:dict(listingId=r.pk,remainingAmount=r.remaining_amount)), \
             patch.object(phase3_marketplace.marketplace_services, "expire_due_listings") as settle:
            data = phase3_marketplace._serialize_query_result("browse_listings", rows, npc)
            # A valid listing remains visible even when one unit costs more than
            # this NPC's balance. Budget and quantity decisions belong to Agent.
            self.assertEqual([r["listingId"] for r in data["listings"]], [7, 8, 9])
            self.assertTrue(all("maxPurchasableAmount" not in r for r in data["listings"]))
            page = phase3_marketplace._apply_pagination(data, 1, 1)
            self.assertEqual(page["total"], 3)
            self.assertTrue(page["has_more"])
            self.assertEqual(page["listings"][0]["listingId"], 7)
            own = phase3_marketplace._serialize_query_result("get_npc_listings", rows[:1], npc)
            self.assertEqual(own["listings"][0]["listingId"], 1)
            settle.assert_not_called()

    def test_ended_goal_keeps_identity_and_result(self):
        for status in ("COMPLETED", "ABANDONED"):
            data = _state_data(SimpleNamespace(status=status, goal_id="goal", goal_version=2))
            self.assertIsNone(data["subjective_goal"])
            self.assertEqual(data["goal_id"], "goal")
            self.assertEqual(data["goal_status"], status)
            self.assertEqual(data["goal_version"], 2)

    def test_detail_uses_role_projection_without_mutating_shared_source(self):
        source = {"story_id": "story", "title": "商路", "phase": "AFTERMATH",
                  "status": "ACTIVE", "story_version": 0, "bridge_condition": "BROKEN",
                  "emergency_route_rule": "Nora支付开通费", "available_actions": [], "progress": {}}
        with patch.dict(agent_queries._STORY_PROJECTORS, {"story": lambda npc: source}), \
             patch.object(agent_queries.VillageStoryArc.objects, "filter") as rows, \
             patch.object(agent_queries, "_world_minute", return_value=100):
            rows.return_value.first.return_value = None
            bill = SimpleNamespace(agent_id="10011", village_profile=SimpleNamespace(capabilities=[]))
            nora = SimpleNamespace(agent_id="10015", village_profile=SimpleNamespace(capabilities=[]))
            self.assertNotIn("emergency_route_rule", agent_queries._story_detail(bill, "story"))
            self.assertIn("emergency_route_rule", agent_queries._story_detail(nora, "story"))
            self.assertIn("emergency_route_rule", source)


class CaravanAgentProjectionTests(SimpleTestCase):
    def test_current_trade_uses_frozen_prices_and_positive_stock(self):
        from village import caravan
        raw = dict(present=True, import_total_amount=3, import_remaining_amount=3, export_total_amount=60, export_remaining_amount=60, can_export=True, location_code="EMERGENCY_TRADE_POST", departure_world_minute=900,
                   next_arrival_world_minute=2100, route_readiness={"route_open": True, "route": "NORMAL"},
                   reason="", stock={"1049": 3, "1050": 0}, import_prices={"1049": 7, "1050": 9},
                   export_prices={"1005": 4}, next_requested_items=[1050])
        with patch.object(caravan, "projection", return_value=raw), \
             patch.object(caravan, "config", return_value=None), \
             patch.object(caravan, "item_pool", return_value=[1049, 1050]), \
             patch.object(caravan.Item.objects, "filter") as items:
            items.return_value.values_list.return_value = [(1005, "小麦"), (1049, "盐"), (1050, "糖")]
            result = caravan.agent_projection(700)
        self.assertEqual(result["caravan"]["location_code"], "EMERGENCY_TRADE_POST")
        self.assertEqual(result["items"], [{"item_id": 1005, "item_name": "小麦", "export_unit_price": 4},
            {"item_id": 1049, "item_name": "盐", "import_available_amount": 3, "import_unit_price": 7}])
        self.assertNotIn("requestable_items", result)
        self.assertEqual(raw["stock"]["1050"], 0)

    def test_closed_route_has_no_current_goods_and_only_conditional_arrival(self):
        from village import caravan
        raw = dict(present=False, can_export=False, next_arrival_world_minute=2100,
                   route_readiness={"route_open": False, "route": "NONE"}, reason="CARAVAN_ROUTE_NOT_OPEN",
                   stock={}, import_prices={}, export_prices={}, next_requested_items=[])
        with patch.object(caravan, "projection", return_value=raw), \
             patch.object(caravan, "config", return_value=None), \
             patch.object(caravan, "item_pool", return_value=[1049]), \
             patch.object(caravan.Item.objects, "filter") as items:
            items.return_value.values_list.return_value = [(1049, "盐")]
            result = caravan.agent_projection(700)
            self.assertEqual(result["items"], [])
            self.assertTrue(result["caravan"]["arrival_conditional"])
            self.assertEqual(result["caravan"]["opening_cost"], 100)
            raw["route_readiness"] = {"route_open": True, "route": "NORMAL"}
            result = caravan.agent_projection(700)
            self.assertEqual(result["caravan"]["status"], "CLOSED")
            self.assertEqual(result["caravan"]["location_code"], "TRADE_ROAD")
            self.assertNotIn("opening_cost", result["caravan"])
