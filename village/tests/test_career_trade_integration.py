"""Necessary production-to-cash and store-ownership checks on isolated SQLite."""
import uuid
from io import StringIO

from django.core.management import call_command
from django.test import override_settings

from inventory.models import NpcGlobalInventorySlot
from village import caravan, caravan_orders
from village.career_catalog import TOMATO_LIVER_SOUP, install_catalog
from village.career_development_models import VillageCareerDevelopment, VillageCareerEvidence
from village.lucy_cooking import cook
from village.models import (NpcProfile, NpcRuntimeState, VillageSimulationConfig,
    VillageStoryArc, VillageStoryFact, VillageStoreInventorySlot, EconomicLedgerEntry)
from village.phase3_asset_service import commit_economy_action
from village.tests import test_phase3_direct_trade as trade_fixture


@override_settings(DEBUG=True, DEPLOYMENT_ENV="development")
class CareerTradeIntegrationTests(trade_fixture.DirectTradeTestBase):
    def setUp(self):
        super().setUp()
        install_catalog()
        self.cfg = VillageSimulationConfig.objects.get(config_key="default")
        self.cfg.career_development_enabled = True
        self.cfg.caravan_orders_enabled = True
        self.cfg.caravan_item_ids = [1049, 1051, 4001]
        self.cfg.caravan_export_item_ids = [1030, 1056, TOMATO_LIVER_SOUP]
        self.cfg.save()
        arc, _ = VillageStoryArc.objects.get_or_create(arc_code="BROKEN_TRADE_ROUTE_V1")
        VillageStoryFact.objects.update_or_create(arc=arc, defaults=dict(bridge_condition="REPAIRED"))
        VillageCareerDevelopment.objects.create(code="LUCY_1", owner_npc_id="10014",
            owner_name="Lucy", title="试营业", location_code="RESTAURANT", status="TRIAL",
            trial_world_minute=100)
        NpcProfile.objects.update_or_create(npc=self.lucy, defaults=dict(display_name="Lucy",
            occupation="restaurateur", home_location_code="RESTAURANT", work_location_code="RESTAURANT",
            social_location_code="VILLAGE_SQUARE", capabilities=["COOK", "PROPOSE_TRADE", "DELIVER_CARAVAN_ORDER"]))
        NpcProfile.objects.update_or_create(npc=self.nora, defaults=dict(display_name="Nora",
            occupation="merchant", home_location_code="GENERAL_STORE", work_location_code="GENERAL_STORE",
            social_location_code="MARKET_STREET", capabilities=["EXPORT_GOODS"]))
        self._give_item(self.bill_inv, 1030, 50)
        self._give_item(self.bill_inv, 1056, 50)
        for item, amount in {1047: 2, 29403: 1, 1049: 1}.items():
            self._give_item(self.lucy_inv, item, amount)

    def minute(self, minute):
        self.time.world_minute = self.time.time_revision = minute
        self.time.save()

    def dish(self):
        self._set_location(10014, "RESTAURANT")
        rid = uuid.uuid4()
        result = cook(10014, {"recipe_id": 29421}, self.time.world_minute, rid)
        self.assertEqual(result["result_code"], "OK", result)
        return self.lucy_inv.slots.get(item_id=TOMATO_LIVER_SOUP, source_request_id=rid)

    def test_trial_demand_precedes_cooking_and_only_proven_output_is_paid_once(self):
        from village.command_catalog import agent_tool
        allowed = agent_tool("PLANT")["parameters"]["properties"]["crop_code"]["enum"]
        self.assertTrue({"TOMATO", "SOYBEAN"}.issubset(allowed))
        order = caravan_orders.ensure_orders(self.time.world_minute)
        self.assertIsNotNone(order)
        line = order.lines.get(item_id=TOMATO_LIVER_SOUP)
        self.assertEqual(line.target_quantity, 3)
        self.assertEqual(order.procurement_snapshot["trial_item_ids"], [TOMATO_LIVER_SOUP])
        frozen = dict(line.quality_prices)
        self.assertFalse(self.lucy_inv.slots.filter(item_id=TOMATO_LIVER_SOUP).exists())
        self.minute(order.windows.first().day_index * 1440 + 660)
        caravan.sync_visit(self.time.world_minute)
        self._set_location(10014, "TRADE_ROAD")
        forged = NpcGlobalInventorySlot.objects.create(bag=self.lucy_inv, slot_index=90,
            item_id=TOMATO_LIVER_SOUP, item_amount=3, quality=3,
            produced_world_minute=self.time.world_minute, source_request_id=uuid.uuid4())
        def deliver(slot, rid):
            return caravan_orders.deliver_order("NPC", "10014", dict(order_id=str(order.pk),
                item_id=TOMATO_LIVER_SOUP, instance_id=str(slot.instance_id), quantity=3),
                self.time.world_minute, rid, "TRADE_ROAD")
        rejected = deliver(forged, uuid.uuid4())
        self.assertEqual(rejected["result_code"], "CARAVAN_ORDER_LOCAL_PRODUCTION_REQUIRED")
        produced = self.dish()
        self._set_location(10014, "TRADE_ROAD")
        view = caravan_orders.orders_overview("NPC", "10014", self.time.world_minute)
        quoted = next(row for row in view["active_order"]["lines"] if row["item_id"] == TOMATO_LIVER_SOUP)
        self.assertEqual(quoted["available_quantity"], 3)
        self.assertEqual(quoted["quality_prices"], frozen)
        self.lucy_inv.refresh_from_db()
        money = self.lucy_inv.money
        rid = uuid.uuid4()
        paid = deliver(produced, rid)
        self.assertEqual(paid["result_code"], "OK", paid)
        self.assertTrue(deliver(produced, rid)["replayed"])
        self.lucy_inv.refresh_from_db()
        self.assertEqual(self.lucy_inv.money, money + 3 * frozen[str(produced.quality)])
        from village.career_development import reserve_trial_production, content_access
        from village.career_trade import forecast_supply
        from village.models import VillageFarmPlot
        VillageCareerDevelopment.objects.create(code="BILL_1", owner_npc_id="10011", owner_name="Bill",
            title="试种", location_code="BILL_FARM", status="TRIAL", trial_world_minute=self.time.world_minute)
        self.assertTrue(reserve_trial_production(1047, 24, uuid.uuid4(), self.time.world_minute))
        VillageFarmPlot.objects.update_or_create(plot_code="FARM_PLOT_A", defaults=dict(
            crop_code="TOMATO", status="GROWING", reclamation_status="READY",
            mature_world_minute=self.time.world_minute + 1440))
        self.assertFalse(content_access(1047)["can_produce"])
        self.assertEqual(forecast_supply(1047, self.time.world_minute, self.time.world_minute + 1500), 4)
        self.assertEqual(VillageCareerDevelopment.objects.get(pk="LUCY_1").status, "COMPLETED")
        self.assertEqual(EconomicLedgerEntry.objects.filter(request_id=rid).count(), 1)
        forged.refresh_from_db()
        self.assertEqual(forged.item_amount, 3)
        self.minute(order.deadline_world_minute)
        caravan_orders.expire_orders(self.time.world_minute)
        self.lucy_inv.refresh_from_db()
        self.assertEqual(self.lucy_inv.money, money + 3 * frozen[str(produced.quality)])

    def test_store_resale_export_pays_store_and_reset_clears_new_progress(self):
        self.cfg.caravan_orders_enabled = False
        self.cfg.save(update_fields=["caravan_orders_enabled"])
        self.minute(700)
        caravan.sync_visit(700)
        produced = self.dish()
        production_id = produced.source_request_id
        self.store_account.balance = 1000
        self.store_account.save(update_fields=["balance"])
        self._set_location(10014, "GENERAL_STORE")
        sale = commit_economy_action(trade_fixture._payload("PROPOSE_TRADE", 10014, "GENERAL_STORE",
            dict(side="SELL", target_npc_id="10015", store_code="GENERAL_STORE",
                item_id=TOMATO_LIVER_SOUP, amount=3)))
        self.assertEqual(sale["result_code"], "OK", sale)
        stock = VillageStoreInventorySlot.objects.get(store=self.store, item_id=TOMATO_LIVER_SOUP)
        self.assertEqual(stock.item_attributes["source_request_id"], str(production_id))
        self.nora_inv.refresh_from_db()
        personal = self.nora_inv.money
        self._set_location(10015, "TRADE_ROAD")
        payload = trade_fixture._payload("EXPORT_GOODS", 10015, "TRADE_ROAD",
            dict(item_id=TOMATO_LIVER_SOUP, amount=3, stock_source="STORE"))
        sold = commit_economy_action(payload)
        self.assertEqual(sold["result_code"], "OK", sold)
        self.assertEqual(sold["data"]["income_account"], "STORE:GENERAL_STORE")
        self.assertTrue(commit_economy_action(payload)["replayed"])
        self.store_account.refresh_from_db()
        self.nora_inv.refresh_from_db()
        self.assertEqual(self.nora_inv.money, personal)
        self.assertEqual(self.store_account.balance, 1000 - sale["data"]["gold_amount"] + sold["external_delta"])
        stock.refresh_from_db()
        self.assertEqual(stock.item_amount, 0)
        self.assertGreater(VillageCareerEvidence.objects.count(), 0)
        from village.tests import test_reset_village_world as reset_fixture
        reset_fixture.ResetVillageWorldTests.setUpTestData.__func__(type(self))
        call_command("reset_village_world", confirm="RESET_WUXI_VILLAGE", stdout=StringIO())
        self.assertEqual(VillageCareerEvidence.objects.count(), 0)
        self.assertFalse(VillageCareerDevelopment.objects.filter(status="COMPLETED").exists())
