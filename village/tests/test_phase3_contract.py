"""Tests for Phase 3 contract constants: action names, param whitelists, result codes."""

from django.test import TestCase

from village.phase3_contract import (
    ACTION_DURATION,
    ACTOR_WHITELIST,
    ALL_ACTION_NAMES,
    ALL_RESULT_CODES,
    ActionName,
    CROP_CODES,
    DELIVERY_POINTS,
    FarmPlotStatus,
    HUNT_CODES,
    INSPECT_STORE_INVENTORY_QUERY,
    ListingStatus,
    LOCATION_REQUIREMENTS,
    PARAM_WHITELIST,
    PLOT_CODES,
    PriceMode,
    RECIPE_WHITELIST,
    ResultCode,
    Side,
    OrderStatus,
    DeliveryStatus,
    DaySnapshotStatus,
    RestaurantStatus,
    StoreStatus,
    StoreOperationalStatus,
    TradeOfferStatus,
    VILLAGE_ECONOMY_ACTION_V1,
    VILLAGE_ECONOMY_RESULT_V1,
)


class SchemaVersionTest(TestCase):
    """Schema versions must be stable."""

    def test_action_schema_version(self):
        self.assertEqual(VILLAGE_ECONOMY_ACTION_V1, "village.economy-action.v1")

    def test_result_schema_version(self):
        self.assertEqual(VILLAGE_ECONOMY_RESULT_V1, "village.economy-result.v1")


class ActionNameTest(TestCase):
    """All 24 action names must be present and stable."""

    def test_all_action_names_count(self):
        self.assertEqual(len(ActionName), 32)

    def test_action_name_values(self):
        expected = {
            "PLANT", "HARVEST", "HUNT", "PROCESS_GAME", "COOK",
            "REQUEST_DISH_REVIEW", "CANCEL_DISH_REVIEW",
            "CREATE_ORDER", "CANCEL_ORDER", "START_DELIVERY",
            "IMPORT_GOODS", "EXPORT_GOODS", "CONTACT_CARAVAN", "WITHDRAW_STORE_FUNDS", "DEPOSIT_STORE_FUNDS", "WITHDRAW_STORE_ITEM", "SET_STORE_LIMITS",
            "RATE_DISH", "PUBLISH_PRICE_INDEX", "AUDIT_PUBLIC_FUND",
            "EAT", "COMMUNITY_MEAL",
            "PROPOSE_TRADE", "RESPOND_TRADE", "CANCEL_TRADE_OFFER",
            "LIST_STORE_ITEM", "UPDATE_STORE_PRICE", "DELIST_STORE_ITEM",
            "BUY_FROM_STORE", "BUY_FROM_MARKETPLACE",
            "SELL_ON_MARKETPLACE", "DELIST_FROM_MARKETPLACE",
        }
        self.assertEqual(ALL_ACTION_NAMES, expected)

    def test_action_name_string_inheritance(self):
        for an in ActionName:
            self.assertIsInstance(an, str)
            # str(Enum) returns "ClassName.MEMBER", but the enum IS a str
            # so it can be compared directly to strings
            self.assertEqual(an, an.value)
            self.assertIsInstance(an.value, str)


class ResultCodeTest(TestCase):
    """All 40 result codes must be present and stable."""

    def test_all_result_codes_count(self):
        self.assertEqual(len(ResultCode), 40)

    def test_result_code_values(self):
        expected = {
            "OK",
            "P3_FEATURE_DISABLED",
            "P3_FORBIDDEN_ACTOR",
            "P3_INVALID_PARAMS",
            "P3_WRONG_LOCATION",
            "P3_ASSET_VERSION_CONFLICT",
            "P3_INSUFFICIENT_ITEM",
            "P3_INSUFFICIENT_MONEY",
            "P3_TOOL_BROKEN",
            "P3_PLOT_NOT_READY",
            "P3_RECIPE_NOT_ALLOWED",
            "P3_ORDER_NOT_OPEN",
            "P3_DELIVERY_NOT_READY",
            "P3_PRICE_UNAVAILABLE",
            "P3_CARAVAN_COOLDOWN",
            "P3_REQUEST_CONFLICT",
            "P3_DAY_NOT_READY",
            "TRADE_TARGET_NOT_FOUND",
            "TRADE_TARGET_REJECTED",
            "TRADE_OFFER_NOT_FOUND",
            "TRADE_OFFER_EXPIRED",
            "TRADE_OFFER_ALREADY_RESOLVED",
            "TRADE_RESPONSE_FORBIDDEN",
            "TRADE_CANCEL_FORBIDDEN",
            "TRADE_NOT_COLOCATED",
            "TRADE_WRONG_LOCATION",
            "TRADE_STORE_CLOSED",
            "TRADE_STORE_FORBIDDEN",
            "TRADE_LISTING_NOT_FOUND",
            "TRADE_LISTING_NOT_ACTIVE",
            "TRADE_LISTING_PRICE_CONFLICT",
            "TRADE_INVALID_CUSTOM_PRICE",
            "TRADE_INVALID_FEE_CONFIG",
            "TRADE_PUBLIC_FUND_NOT_FOUND",
            "TRADE_ITEM_NOT_FOR_SALE",
            "TRADE_INSUFFICIENT_MONEY",
            "TRADE_INSUFFICIENT_ITEM",
            "TRADE_ASSET_VERSION_CONFLICT",
            "TRADE_STORE_VERSION_CONFLICT",
            "TRADE_REQUEST_CONFLICT",
        }
        self.assertEqual(ALL_RESULT_CODES, expected)


class ParamWhitelistTest(TestCase):
    """Every action must have a param whitelist entry."""

    def test_all_actions_have_whitelist(self):
        for an in ActionName:
            self.assertIn(an, PARAM_WHITELIST, f"Missing whitelist for {an}")

    def test_plant_params(self):
        self.assertEqual(
            PARAM_WHITELIST[ActionName.PLANT],
            frozenset({"plot_code", "crop_code", "expected_asset_version"}),
        )

    def test_harvest_params(self):
        self.assertEqual(
            PARAM_WHITELIST[ActionName.HARVEST],
            frozenset({"plot_code", "expected_asset_version"}),
        )

    def test_hunt_params(self):
        self.assertEqual(
            PARAM_WHITELIST[ActionName.HUNT],
            frozenset({"hunt_code", "expected_asset_version"}),
        )

    def test_cook_params(self):
        self.assertEqual(
            PARAM_WHITELIST[ActionName.COOK],
            frozenset({"recipe_id", "expected_asset_version"}),
        )

    def test_create_order_params(self):
        expected = frozenset({
            "side", "item_id", "amount", "unit_price",
            "delivery_point_code", "expires_world_minute",
            "expected_asset_version",
        })
        self.assertEqual(PARAM_WHITELIST[ActionName.CREATE_ORDER], expected)

    def test_cancel_order_params(self):
        self.assertEqual(
            PARAM_WHITELIST[ActionName.CANCEL_ORDER],
            frozenset({"order_id"}),
        )

    def test_start_delivery_params(self):
        expected = frozenset({
            "delivery_id", "buyer_npc_id", "seller_npc_id",
            "delivery_point_code",
        })
        self.assertEqual(PARAM_WHITELIST[ActionName.START_DELIVERY], expected)

    def test_import_goods_params(self):
        self.assertEqual(
            PARAM_WHITELIST[ActionName.IMPORT_GOODS],
            frozenset({"items", "item_id", "amount", "store_code"}),
        )

    def test_export_goods_params(self):
        self.assertEqual(
            PARAM_WHITELIST[ActionName.EXPORT_GOODS],
            frozenset({"item_id", "amount", "expected_asset_version"}),
        )

    def test_rate_dish_params(self):
        self.assertEqual(
            PARAM_WHITELIST[ActionName.RATE_DISH],
            frozenset({"review_id", "scores", "comment", "customer_feedback"}),
        )

    def test_publish_price_index_params(self):
        self.assertEqual(
            PARAM_WHITELIST[ActionName.PUBLISH_PRICE_INDEX],
            frozenset({"day_index"}),
        )

    def test_audit_public_fund_params(self):
        self.assertEqual(
            PARAM_WHITELIST[ActionName.AUDIT_PUBLIC_FUND],
            frozenset({"day_index"}),
        )


class ActorWhitelistTest(TestCase):
    """Every action must have an actor whitelist entry."""

    def test_all_actions_have_actor_whitelist(self):
        for an in ActionName:
            self.assertIn(an, ACTOR_WHITELIST, f"Missing actor whitelist for {an}")

    def test_plant_actor(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.PLANT], {"10011"})

    def test_hunt_actor(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.HUNT], {"10012"})

    def test_cook_actor(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.COOK], {"10014"})

    def test_import_export_actor(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.IMPORT_GOODS], {"10015"})
        self.assertEqual(ACTOR_WHITELIST[ActionName.EXPORT_GOODS], {"10015"})

    def test_mark_actions(self):
        for action in (ActionName.RATE_DISH, ActionName.PUBLISH_PRICE_INDEX, ActionName.AUDIT_PUBLIC_FUND):
            self.assertEqual(ACTOR_WHITELIST[action], {"10013"})

    def test_create_order_multiple_actors(self):
        self.assertEqual(
            ACTOR_WHITELIST[ActionName.CREATE_ORDER],
            {"10011", "10012", "10014", "10015"},
        )


class LocationRequirementTest(TestCase):
    """Every action must have a location requirement entry."""

    def test_all_actions_have_location(self):
        for an in ActionName:
            self.assertIn(an, LOCATION_REQUIREMENTS)

    def test_plant_location(self):
        self.assertEqual(LOCATION_REQUIREMENTS[ActionName.PLANT], "BILL_FARM")

    def test_hunt_location(self):
        self.assertEqual(LOCATION_REQUIREMENTS[ActionName.HUNT], "HUNTING_GATE")

    def test_cook_location(self):
        self.assertEqual(LOCATION_REQUIREMENTS[ActionName.COOK], "RESTAURANT")

    def test_order_location_flexible(self):
        self.assertIsNone(LOCATION_REQUIREMENTS[ActionName.CREATE_ORDER])
        self.assertIsNone(LOCATION_REQUIREMENTS[ActionName.CANCEL_ORDER])


class DurationTest(TestCase):
    """Every action must have a duration entry."""

    def test_all_actions_have_duration(self):
        for an in ActionName:
            if an in {ActionName.HUNT, ActionName.PROCESS_GAME}:
                self.assertNotIn(an, ACTION_DURATION)
            else:
                self.assertIn(an, ACTION_DURATION)

    def test_plant_duration(self):
        self.assertEqual(ACTION_DURATION[ActionName.PLANT], 60)

    def test_hunt_duration(self):
        self.assertNotIn(ActionName.HUNT, ACTION_DURATION)

    def test_cook_duration(self):
        self.assertEqual(ACTION_DURATION[ActionName.COOK], 90)

    def test_order_duration(self):
        self.assertEqual(ACTION_DURATION[ActionName.CREATE_ORDER], 1)


class EnumStabilityTest(TestCase):
    """Frozen enum values must not change."""

    def test_plot_codes(self):
        self.assertEqual(len(PLOT_CODES), 36)
        self.assertEqual(PLOT_CODES[:3], ("FARM_PLOT_A", "FARM_PLOT_A_02", "FARM_PLOT_A_03"))
        self.assertIn("FARM_PLOT_B", PLOT_CODES)
        self.assertIn("FARM_PLOT_C", PLOT_CODES)
        self.assertIn("FARM_PLOT_D_09", PLOT_CODES)

    def test_crop_codes(self):
        self.assertEqual(CROP_CODES, ("CORN", "WHEAT", "CARROT", "POTATO"))

    def test_hunt_codes(self):
        self.assertEqual(HUNT_CODES, ("HUNT_ANIMALS",))

    def test_delivery_points(self):
        self.assertEqual(DELIVERY_POINTS, ("MARKET_STREET", "RESTAURANT", "GENERAL_STORE"))

    def test_recipe_whitelist(self):
        self.assertTrue({2, 3, 6, 12, 16, 27}.issubset(RECIPE_WHITELIST))

    def test_side_enum(self):
        self.assertEqual(Side.BUY, "BUY")
        self.assertEqual(Side.SELL, "SELL")

    def test_order_statuses(self):
        expected = {"OPEN", "MATCHED", "FILLED", "CANCELLED", "EXPIRED"}
        self.assertEqual({s.value for s in OrderStatus}, expected)

    def test_delivery_statuses(self):
        expected = {"ACTIVE", "COMPLETED", "CANCELLED", "EXPIRED"}
        self.assertEqual({s.value for s in DeliveryStatus}, expected)

    def test_farm_plot_statuses(self):
        expected = {"EMPTY", "GROWING", "MATURE"}
        self.assertEqual({s.value for s in FarmPlotStatus}, expected)

    def test_day_snapshot_statuses(self):
        expected = {"DRAFT", "PUBLISHED"}
        self.assertEqual({s.value for s in DaySnapshotStatus}, expected)

    def test_restaurant_statuses(self):
        expected = {"OPEN", "LOW_STOCK", "SOLD_OUT"}
        self.assertEqual({s.value for s in RestaurantStatus}, expected)

    def test_store_statuses(self):
        expected = {"NORMAL", "LOW_STOCK", "OUT_OF_STOCK"}
        self.assertEqual({s.value for s in StoreStatus}, expected)

    def test_store_operational_statuses(self):
        expected = {"OPEN", "CLOSED", "SUSPENDED"}
        self.assertEqual({s.value for s in StoreOperationalStatus}, expected)

    def test_listing_statuses(self):
        expected = {"ACTIVE", "SOLD_OUT", "DELISTED"}
        self.assertEqual({s.value for s in ListingStatus}, expected)

    def test_price_modes(self):
        expected = {"ORIGINAL", "CUSTOM"}
        self.assertEqual({s.value for s in PriceMode}, expected)

    def test_trade_offer_statuses(self):
        expected = {"PENDING", "ACCEPTED", "REJECTED", "CANCELLED", "EXPIRED"}
        self.assertEqual({s.value for s in TradeOfferStatus}, expected)


class Phase3TradeActionContractTest(TestCase):
    """The 10 Phase 3 direct-trade commands must be fully frozen."""

    def test_propose_trade_contract(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.PROPOSE_TRADE], set())
        self.assertIsNone(LOCATION_REQUIREMENTS[ActionName.PROPOSE_TRADE])
        self.assertEqual(ACTION_DURATION[ActionName.PROPOSE_TRADE], 1)
        self.assertEqual(
            PARAM_WHITELIST[ActionName.PROPOSE_TRADE],
            frozenset({
                "target_npc_id", "side", "item_id", "amount", "gold_amount",
                "conversation_id", "expected_seller_asset_version", "expires_world_minute", "commitment_id", "store_code",
            }),
        )

    def test_respond_trade_contract(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.RESPOND_TRADE], set())
        self.assertIsNone(LOCATION_REQUIREMENTS[ActionName.RESPOND_TRADE])
        self.assertEqual(ACTION_DURATION[ActionName.RESPOND_TRADE], 1)
        self.assertEqual(
            PARAM_WHITELIST[ActionName.RESPOND_TRADE],
            frozenset({"offer_id", "decision"}),
        )

    def test_cancel_trade_offer_contract(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.CANCEL_TRADE_OFFER], set())
        self.assertIsNone(LOCATION_REQUIREMENTS[ActionName.CANCEL_TRADE_OFFER])
        self.assertEqual(ACTION_DURATION[ActionName.CANCEL_TRADE_OFFER], 1)
        self.assertEqual(
            PARAM_WHITELIST[ActionName.CANCEL_TRADE_OFFER],
            frozenset({"offer_id"}),
        )

    def test_list_store_item_contract(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.LIST_STORE_ITEM], {"10014", "10015"})
        self.assertIsNone(LOCATION_REQUIREMENTS[ActionName.LIST_STORE_ITEM])
        self.assertEqual(ACTION_DURATION[ActionName.LIST_STORE_ITEM], 1)
        self.assertEqual(
            PARAM_WHITELIST[ActionName.LIST_STORE_ITEM],
            frozenset({
                "store_code", "item_id", "amount", "price_mode", "custom_unit_price", "stock_source",
                "expected_operator_asset_version", "expected_store_inventory_version",
                "food_instance_id", "public_meal_code", "decision_id",
            }),
        )

    def test_update_store_price_contract(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.UPDATE_STORE_PRICE], {"10014", "10015"})
        self.assertIsNone(LOCATION_REQUIREMENTS[ActionName.UPDATE_STORE_PRICE])
        self.assertEqual(ACTION_DURATION[ActionName.UPDATE_STORE_PRICE], 1)
        self.assertEqual(
            PARAM_WHITELIST[ActionName.UPDATE_STORE_PRICE],
            frozenset({
                "listing_id", "price_mode", "custom_unit_price", "expected_store_inventory_version",
            }),
        )

    def test_delist_store_item_contract(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.DELIST_STORE_ITEM], {"10014", "10015"})
        self.assertIsNone(LOCATION_REQUIREMENTS[ActionName.DELIST_STORE_ITEM])
        self.assertEqual(ACTION_DURATION[ActionName.DELIST_STORE_ITEM], 1)
        self.assertEqual(
            PARAM_WHITELIST[ActionName.DELIST_STORE_ITEM],
            frozenset({
                "listing_id", "expected_operator_asset_version", "expected_store_inventory_version",
            }),
        )

    def test_buy_from_store_contract(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.BUY_FROM_STORE], set())
        self.assertIsNone(LOCATION_REQUIREMENTS[ActionName.BUY_FROM_STORE])
        self.assertEqual(ACTION_DURATION[ActionName.BUY_FROM_STORE], 1)
        self.assertEqual(
            PARAM_WHITELIST[ActionName.BUY_FROM_STORE],
            frozenset({
                "listing_id", "amount", "expected_store_inventory_version", "expected_buyer_asset_version",
            }),
        )

    def test_buy_from_marketplace_contract(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.BUY_FROM_MARKETPLACE], set())
        self.assertEqual(LOCATION_REQUIREMENTS[ActionName.BUY_FROM_MARKETPLACE], "MARKET_STREET")
        self.assertEqual(ACTION_DURATION[ActionName.BUY_FROM_MARKETPLACE], 1)
        self.assertEqual(
            PARAM_WHITELIST[ActionName.BUY_FROM_MARKETPLACE],
            frozenset({"listing_id", "amount", "expected_buyer_asset_version"}),
        )

    def test_sell_on_marketplace_contract(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.SELL_ON_MARKETPLACE], set())
        self.assertEqual(LOCATION_REQUIREMENTS[ActionName.SELL_ON_MARKETPLACE], "MARKET_STREET")
        self.assertEqual(ACTION_DURATION[ActionName.SELL_ON_MARKETPLACE], 1)
        self.assertEqual(
            PARAM_WHITELIST[ActionName.SELL_ON_MARKETPLACE],
            frozenset({"instance_id", "amount", "price", "expected_asset_version"}),
        )

    def test_delist_from_marketplace_contract(self):
        self.assertEqual(ACTOR_WHITELIST[ActionName.DELIST_FROM_MARKETPLACE], set())
        self.assertEqual(LOCATION_REQUIREMENTS[ActionName.DELIST_FROM_MARKETPLACE], "MARKET_STREET")
        self.assertEqual(ACTION_DURATION[ActionName.DELIST_FROM_MARKETPLACE], 1)
        self.assertEqual(
            PARAM_WHITELIST[ActionName.DELIST_FROM_MARKETPLACE],
            frozenset({"listing_id", "expected_asset_version"}),
        )

    def test_all_trade_actions_have_full_contract_entries(self):
        for action in (
            ActionName.PROPOSE_TRADE,
            ActionName.RESPOND_TRADE,
            ActionName.CANCEL_TRADE_OFFER,
            ActionName.LIST_STORE_ITEM,
            ActionName.UPDATE_STORE_PRICE,
            ActionName.DELIST_STORE_ITEM,
            ActionName.BUY_FROM_STORE,
            ActionName.BUY_FROM_MARKETPLACE,
            ActionName.SELL_ON_MARKETPLACE,
            ActionName.DELIST_FROM_MARKETPLACE,
        ):
            self.assertIn(action, ACTOR_WHITELIST)
            self.assertIn(action, LOCATION_REQUIREMENTS)
            self.assertIn(action, ACTION_DURATION)
            self.assertIn(action, PARAM_WHITELIST)


class InspectStoreInventoryQueryTest(TestCase):
    """INSPECT_STORE_INVENTORY is a read-only query, not a command."""

    def test_query_constant_value(self):
        self.assertEqual(INSPECT_STORE_INVENTORY_QUERY, "INSPECT_STORE_INVENTORY")

    def test_query_is_not_an_action_name(self):
        self.assertNotIn(INSPECT_STORE_INVENTORY_QUERY, ALL_ACTION_NAMES)
        for an in ActionName:
            self.assertNotEqual(an.value, INSPECT_STORE_INVENTORY_QUERY)

    def test_query_has_no_settlement_contract_entries(self):
        for an in ActionName:
            self.assertNotEqual(an, INSPECT_STORE_INVENTORY_QUERY)
        self.assertNotIn(INSPECT_STORE_INVENTORY_QUERY, ACTOR_WHITELIST)
        self.assertNotIn(INSPECT_STORE_INVENTORY_QUERY, LOCATION_REQUIREMENTS)
        self.assertNotIn(INSPECT_STORE_INVENTORY_QUERY, ACTION_DURATION)
        self.assertNotIn(INSPECT_STORE_INVENTORY_QUERY, PARAM_WHITELIST)
