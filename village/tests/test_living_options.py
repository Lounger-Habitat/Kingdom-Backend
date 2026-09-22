import importlib
import uuid
import time
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase, override_settings

from inventory.models import NpcDefinition
from items.models import Item, ItemType
from village.agent_queries import execute_agent_query
from village.models import (
    NpcProfile, NpcRuntimeState, VillageLocationDefinition, VillageStoreListing,
    VillageStoreInventorySlot, VillageSimulationConfig,
)
from village.runtime_services import CommandService
from village.seed_data import NPC_SEEDS
from village.tests import test_phase3_direct_trade as trade_tests


class FoodAccessTestBase(trade_tests.DirectTradeTestBase):
    def setUp(self):
        super().setUp()
        Item.objects.filter(item_id=1030).update(item_type=ItemType.FOOD)
        Item.objects.filter(item_id=4001).update(item_type=ItemType.HOE_TOOL)
        self._give_item(self.nora_inv, 1030, 5, produced=600, expires=2000)
        self.food_listing = self._list(10015, "GENERAL_STORE", 1030, 5, "ORIGINAL")["data"]["listing_id"]
        self._set_location(10011, "GENERAL_STORE")
        NpcRuntimeState.objects.filter(npc=self.bill).update(health_fp=300)


class LowHealthFoodTests(FoodAccessTestBase):
    def test_low_health_buys_real_food_and_keeps_money_and_stock_checks(self):
        result = self._buy(10011, self.food_listing, 1)
        self.assertEqual(result["result_code"], "OK", result)
        self.bill_inv.refresh_from_db()
        self.assertEqual(self.bill_inv.money, 192)
        self.assertEqual(self.bill_inv.slots.get(item_id=1030).item_amount, 1)
        self.assertEqual(self._buy(10011, self.food_listing, 10)["result_code"], "TRADE_INSUFFICIENT_ITEM")
        self.bill_inv.money = 0
        self.bill_inv.save(update_fields=["money"])
        self.assertEqual(self._buy(10011, self.food_listing, 1)["result_code"], "TRADE_INSUFFICIENT_MONEY")

    def test_low_health_nonfood_is_rejected_before_assets_change(self):
        self._give_item(self.nora_inv, 1049, 1)
        listing_id = self._list(10015, "GENERAL_STORE", 1049, 1, "ORIGINAL")["data"]["listing_id"]
        slot = VillageStoreInventorySlot.objects.get(listing_id=listing_id)
        runtime = NpcRuntimeState.objects.get(npc=self.bill)
        self.assertEqual(CommandService.unavailable_reason("BUY_FROM_STORE", runtime,
            {"listing_id": listing_id}), "HEALTH_TOO_LOW")
        self.assertIsNone(CommandService.unavailable_reason("BUY_FROM_STORE", runtime,
            {"listing_id": self.food_listing}))
        self.assertEqual(CommandService.unavailable_reason("PLANT", runtime), "HEALTH_TOO_LOW")
        result = self._buy(10011, listing_id, 1)
        self.assertEqual(result["result_code"], "HEALTH_TOO_LOW", result)
        slot.refresh_from_db(); self.bill_inv.refresh_from_db()
        self.assertEqual(slot.item_amount, 1)
        self.assertEqual(self.bill_inv.money, 200)

    @override_settings(VILLAGE_FAST_COMMAND_INTEGRATION=False)
    def test_command_lifecycle_allows_food_but_rejects_nonfood_at_start(self):
        self._give_item(self.nora_inv, 1049, 1)
        salt = self._list(10015, "GENERAL_STORE", 1049, 1, "ORIGINAL")["data"]["listing_id"]
        NpcProfile.objects.update_or_create(npc=self.bill, defaults=dict(display_name="Bill", capabilities=["BUY_FROM_STORE"]))
        NpcRuntimeState.objects.filter(npc=self.bill).update(needs_world_minute=700)
        for listing_id, expected in ((salt, "HEALTH_TOO_LOW"), (self.food_listing, "COMMAND_ACCEPTED")):
            runtime = NpcRuntimeState.objects.get(npc=self.bill)
            accepted, code, command = CommandService.accept_command(str(uuid.uuid4()), "10011", "env", {
                "schema_version": "village.command.v1", "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()),
                "name": "BUY_FROM_STORE", "params": {"listing_id": listing_id, "amount": 1},
                "queue_mode": "STRICT", "expected_state_version": runtime.state_version, "ttl_ms": 30000,
            }, int(time.time() * 1000))
            self.assertTrue(accepted, code)
            started, code, _ = CommandService.start_command(command.command_id)
            self.assertEqual(code, expected)
            self.assertEqual(started, expected == "COMMAND_ACCEPTED")

    def test_healthy_npc_keeps_nonfood_purchase_permission(self):
        NpcRuntimeState.objects.filter(npc=self.bill).update(health_fp=301)
        self._give_item(self.nora_inv, 1049, 1)
        listing = self._list(10015, "GENERAL_STORE", 1049, 1, "ORIGINAL")["data"]["listing_id"]
        self.assertEqual(self._buy(10011, listing, 1)["result_code"], "OK")


class LivingOptionsTests(FoodAccessTestBase):
    def setUp(self):
        super().setUp()
        NpcProfile.objects.update_or_create(npc=self.bill, defaults=dict(display_name="Bill",
            occupation="farmer", home_location_code="BILL_HOME", work_location_code="BILL_FARM",
            social_location_code="RESTAURANT", capabilities=["GET_RULE", "INSPECT_LIVING_OPTIONS", "BUY_FROM_STORE", "REST", "SLEEP"]))
        for code in ("BILL_HOME", "GENERAL_STORE", "RESTAURANT"):
            VillageLocationDefinition.objects.update_or_create(location_code=code,
                defaults=dict(display_name=code, center_anchor_code=code, is_public=code != "BILL_HOME"))
        self._set_location(10011, "BILL_HOME")

    def query(self):
        ok, code, data = execute_agent_query("10011", "INSPECT_LIVING_OPTIONS")
        self.assertTrue(ok, code)
        return data

    def test_remote_summary_is_read_only_and_hides_batches_and_accounts(self):
        with connection.execute_wrapper(self._reject_writes):
            data = self.query()
        source = next(s for s in data["food_sources"] if s["store_code"] == "GENERAL_STORE")
        self.assertTrue(source["requires_move"])
        self.assertTrue(source["available"])
        self.assertEqual(source["foods"][0]["unit_price"], 8)
        for private in ("listing_id", "batches", "food_instance_id", "account_balance", "inventory_version"):
            self.assertNotIn(private, str(source))
        home = next(p for p in data["rest_places"] if p["location_code"] == "BILL_HOME")
        self.assertEqual({a["action"] for a in home["actions"]}, {"REST", "SLEEP"})

    @staticmethod
    def _reject_writes(execute, sql, params, many, context):
        if sql.lstrip().split()[0].upper() not in {"SELECT", "PRAGMA"}:
            raise AssertionError("Read-only query attempted write: " + sql)
        return execute(sql, params, many, context)

    def test_unaffordable_expired_closed_and_disabled_sources_explain_obstacles(self):
        self.bill_inv.money = 0; self.bill_inv.save(update_fields=["money"])
        food = next(s for s in self.query()["food_sources"] if s["store_code"] == "GENERAL_STORE")["foods"][0]
        self.assertFalse(food["available"])
        self.assertIn("TRADE_INSUFFICIENT_MONEY", food["blocked_reasons"])
        VillageStoreInventorySlot.objects.filter(listing_id=self.food_listing).update(expires_world_minute=699)
        food = next(s for s in self.query()["food_sources"] if s["store_code"] == "GENERAL_STORE")["foods"][0]
        self.assertEqual(food["available_amount"], 0)
        self.assertIn("TRADE_INSUFFICIENT_ITEM", food["blocked_reasons"])
        self.store.status = "CLOSED"; self.store.save(update_fields=["status"])
        source = next(s for s in self.query()["food_sources"] if s["store_code"] == "GENERAL_STORE")
        self.assertIn("TRADE_STORE_CLOSED", source["blocked_reasons"])
        VillageSimulationConfig.objects.update(village_direct_trade_enabled=False)
        source = next(s for s in self.query()["food_sources"] if s["store_code"] == "GENERAL_STORE")
        self.assertIn("P3_FEATURE_DISABLED", source["blocked_reasons"])

    def test_closed_restaurant_and_private_home_are_not_offered_as_free_access(self):
        VillageLocationDefinition.objects.filter(location_code="RESTAURANT").update(opening_hours={"alwaysOpen": False})
        VillageLocationDefinition.objects.create(location_code="OTHER_HOME", display_name="private",
            center_anchor_code="OTHER_HOME", is_public=False)
        data = self.query()
        restaurant = next(p for p in data["rest_places"] if p["location_code"] == "RESTAURANT")
        self.assertEqual(restaurant["actions"], [{"action": "REST", "available": False, "blocked_reasons": ["LOCATION_CLOSED"]}])
        self.assertNotIn("OTHER_HOME", str(data))


class LivingCapabilitiesTests(TestCase):
    def test_seed_permissions_and_idempotent_migration_preserve_other_capabilities(self):
        for seed in NPC_SEEDS:
            self.assertTrue({"GET_RULE", "INSPECT_LIVING_OPTIONS"}.issubset(seed["capabilities"]))
            self.assertNotIn("WORK_PLACEHOLDER", seed["capabilities"])
        npc = NpcDefinition.objects.create(agent_id="10011", name="bill")
        profile = NpcProfile.objects.create(npc=npc, display_name="Bill", capabilities=["PLANT", "WORK_PLACEHOLDER"])
        migrate = importlib.import_module("village.migrations.0056_living_resource_capabilities").update_capabilities
        for _ in range(2):
            migrate(apps, SimpleNamespace(connection=connection))
        profile.refresh_from_db()
        self.assertEqual(profile.capabilities, ["PLANT", "GET_RULE", "INSPECT_LIVING_OPTIONS"])

    def test_formal_npc_can_discover_and_query_rules_even_with_old_profile(self):
        npc = NpcDefinition.objects.create(agent_id="10011", name="bill")
        NpcProfile.objects.create(npc=npc, display_name="Bill", capabilities=[])
        self.assertIn("GET_RULE", CommandService.allowed_actions_for(npc))
        ok, code, data = execute_agent_query("10011", "GET_RULE")
        self.assertTrue(ok, code)
        self.assertIn("common_rules", data)
