"""Configurable trade quotas, frozen prices and the split agent queries."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from items.models import Item
from village import caravan
from village.admin import VillageCaravanConfigForm
from village.agent_queries import execute_agent_query
from village.command_catalog import agent_tool
from village.models import (NpcProfile, VillageCaravanConfig, VillageCaravanVisit,
                            VillageSimulationConfig, VillageStoryArc, VillageStoryFact)
from village.phase3_asset_service import commit_economy_action
from village.tests.test_phase3_direct_trade import DirectTradeTestBase, _payload


class CaravanConfigurationTests(DirectTradeTestBase):
    def setUp(self):
        super().setUp()
        self.cfg = VillageSimulationConfig.objects.get(config_key="default")
        self.cfg.caravan_item_ids = [1049, 1056]
        self.cfg.caravan_export_item_ids = [1030, 1049]
        self.cfg.caravan_emergency_capacity = 7
        self.cfg.caravan_export_capacity = 3
        self.cfg.save()
        arc = VillageStoryArc.objects.create(arc_code="BROKEN_TRADE_ROUTE_V1", current_phase="EXECUTION")
        VillageStoryFact.objects.create(arc=arc, emergency_caravan_succeeded=True)
        NpcProfile.objects.update_or_create(npc=self.nora, defaults={"display_name": "Nora",
            "is_active": True, "capabilities": ["INSPECT_MARKET_STATE", "INSPECT_CARAVAN_REQUESTS", "CONTACT_CARAVAN"]})
        self.clock(650)

    def clock(self, minute):
        self.time.world_minute = minute
        self.time.time_revision = minute
        self.time.save()

    def arrive(self, minute=660):
        self.clock(minute)
        return caravan.sync_visit(minute)

    def act(self, action, params):
        return commit_economy_action(_payload(action, 10015, "GENERAL_STORE", params))

    def query(self, action, params=None):
        success, code, data = execute_agent_query("10015", action, params)
        self.assertTrue(success, code)
        return data

    def test_complete_request_catalog_target_time_and_replacement(self):
        before = self.query("INSPECT_CARAVAN_REQUESTS")
        self.assertEqual([r["item_id"] for r in before["requestable_items"]], [1049, 1056])
        self.assertEqual(before["arrival_world_minute"], 660)
        self.assertEqual(before["import_total_amount"], 7)
        self.assertFalse(VillageCaravanVisit.objects.exists())
        first = self.act("CONTACT_CARAVAN", {"item_ids": [1049, 1056]})
        self.assertEqual(first["result_code"], "OK")
        self.assertEqual(first["data"]["import_total_amount"], 7)
        self.assertEqual(first["data"]["requested_items"], [{"item_id": 1049, "item_name": "盐"}, {"item_id": 1056, "item_name": "鸡肉"}])
        self.act("CONTACT_CARAVAN", {"item_ids": [1056]})
        rows = self.query("INSPECT_CARAVAN_REQUESTS")["requestable_items"]
        self.assertNotIn("requested", rows[0])
        self.assertTrue(rows[1]["requested"])
        visit = self.arrive()
        self.assertEqual(visit.stock, {"1056": 7})  # chicken is outside the old import whitelist
        after = self.query("INSPECT_CARAVAN_REQUESTS")
        self.assertEqual(after["arrival_world_minute"], 2100)
        self.assertFalse(any(row.get("requested") for row in after["requestable_items"]))
        self.assertEqual(agent_tool("INSPECT_CARAVAN_REQUESTS")["tags"], ["read"])

    def test_fixed_total_with_and_without_requests(self):
        caravan.save_demand([1049, 1056], 650)
        visit = self.arrive()
        self.assertEqual(sum(visit.stock.values()), 7)
        self.assertTrue(all(n >= 1 for n in visit.stock.values()))
        self.assertEqual(sum(self.arrive(2100).stock.values()), 7)

    def test_current_market_has_no_requests_and_keeps_quotas_when_filtered(self):
        caravan.save_demand([1056], 650)
        self.arrive()
        state = self.query("INSPECT_MARKET_STATE", {"page_size": 1})
        self.assertNotIn("requestable_items", state)
        self.assertEqual(state["total"], 3)
        self.assertEqual(state["next_page"], 2)
        self.assertEqual(state["caravan"]["import_total_amount"], 7)
        self.assertEqual(state["caravan"]["export_remaining_amount"], 3)
        # Requestable but absent from this visit is not a market item.
        self.cfg.caravan_item_ids.append(4001)
        self.cfg.save()
        missing = self.query("INSPECT_MARKET_STATE", {"item_id": 4001})
        self.assertFalse(missing["item_found"])
        self.assertEqual(missing["caravan"]["export_remaining_amount"], 3)

    def test_quota_shared_across_kinds_and_replay_failure_are_atomic(self):
        visit = self.arrive()
        self._give_item(self.nora_inv, 1030, 20)
        self._give_item(self.nora_inv, 1049, 30)
        payload = _payload("EXPORT_GOODS", 10015, "EMERGENCY_TRADE_POST", {"item_id": 1030, "amount": 2})
        first = commit_economy_action(payload)
        self.assertEqual(first["result_code"], "OK")
        self.assertTrue(commit_economy_action(payload)["replayed"])
        self.nora_inv.refresh_from_db()
        balance = self.nora_inv.money
        failed = self.act("EXPORT_GOODS", {"item_id": 1049, "amount": 2})
        self.assertEqual(failed["result_code"], "CARAVAN_EXPORT_CAPACITY_EXCEEDED")
        self.assertEqual(failed["data"]["export_remaining_amount"], 1)
        visit.refresh_from_db()
        self.nora_inv.refresh_from_db()
        self.assertEqual((visit.exported_amount, self.nora_inv.money), (2, balance))
        self.assertEqual(self.act("EXPORT_GOODS", {"item_id": 1049, "amount": 1})["result_code"], "OK")
        raw = caravan.projection(660)
        self.assertFalse(raw["can_export"])
        state = self.query("INSPECT_MARKET_STATE")
        self.assertEqual(state["caravan"]["status"], "PRESENT")
        self.assertEqual(state["caravan"]["export_remaining_amount"], 0)
        self.assertTrue(any("export_unit_price" in row for row in state["items"]))
        self.assertEqual(self.arrive(2100).exported_amount, 0)

    def test_failed_inventory_or_safety_check_does_not_consume_quota(self):
        visit = self.arrive()
        self._give_item(self.nora_inv, 1030, 3)
        self.assertEqual(self.act("EXPORT_GOODS", {"item_id": 1030, "amount": 3})["result_code"], "EXPORT_SAFETY_STOCK_REQUIRED")
        visit.refresh_from_db()
        self.assertEqual(visit.exported_amount, 0)

    @patch("village.phase3_public_economy._get_published_price", return_value=100)
    def test_price_range_and_config_changes_only_affect_next_visit(self, price):
        caravan.save_demand([1056], 650)
        visit = self.arrive()
        original_prices = dict(visit.export_prices)
        self.assertTrue(all(90 <= p <= 110 for p in [*visit.import_prices.values(), *visit.export_prices.values()]))
        self.cfg.caravan_item_ids = [4001]
        self.cfg.caravan_export_item_ids = [1056]
        self.cfg.caravan_export_capacity = 9
        self.cfg.caravan_emergency_capacity = 4
        self.cfg.caravan_import_price_min_percent = self.cfg.caravan_import_price_max_percent = 120
        self.cfg.caravan_export_price_min_percent = self.cfg.caravan_export_price_max_percent = 80
        self.cfg.save()
        state = self.query("INSPECT_MARKET_STATE")
        self.assertEqual(state["caravan"]["export_total_amount"], 3)
        self.assertEqual({r["item_id"] for r in state["items"]}, {1030, 1049, 1056})
        self.assertEqual(caravan.sync_visit(700).export_prices, original_prices)
        self.assertEqual(self.act("IMPORT_GOODS", {"item_id": 1056, "amount": 1})["result_code"], "OK")
        self._give_item(self.nora_inv, 1049, 30)
        self.assertEqual(self.act("EXPORT_GOODS", {"item_id": 1049, "amount": 1})["result_code"], "OK")
        tomorrow = self.arrive(2100)
        self.assertEqual(tomorrow.stock, {"4001": 4})
        self.assertEqual(tomorrow.export_capacity, 9)
        self.assertEqual(tomorrow.import_prices, {"4001": 120})
        self.assertEqual(tomorrow.export_prices, {"1056": 80})

    def test_disabled_export_and_deleted_catalog_items(self):
        self.cfg.caravan_export_capacity = 0
        self.cfg.save()
        Item.objects.filter(item_id=1056).update(is_deleted=True)
        rows = self.query("INSPECT_CARAVAN_REQUESTS")["requestable_items"]
        self.assertEqual([r["item_id"] for r in rows], [1049])
        self.assertEqual(self.act("CONTACT_CARAVAN", {"item_ids": [1056]})["result_code"], "CARAVAN_ITEM_UNAVAILABLE")
        self.arrive()
        self.assertEqual(self.act("EXPORT_GOODS", {"item_id": 1030, "amount": 1})["result_code"], "CARAVAN_EXPORT_CAPACITY_EXCEEDED")

    def test_admin_page_and_configuration_validation(self):
        user = get_user_model().objects.create_superuser(username="caravan-admin", password="test-only")
        self.client.force_login(user)
        url = reverse("admin:village_villagecaravanconfig_change", args=[self.cfg.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "出口收购总量")
        config = VillageCaravanConfig.objects.get(pk=self.cfg.pk)
        fields = VillageCaravanConfigForm.Meta.fields
        data = {name: getattr(config, name) for name in fields}
        form = VillageCaravanConfigForm(data, instance=config)
        self.assertTrue(form.is_valid(), form.errors)
        data["caravan_export_price_min_percent"] = 120
        data["caravan_export_price_max_percent"] = 80
        form = VillageCaravanConfigForm(data, instance=config)
        self.assertFalse(form.is_valid())
        self.assertIn("caravan_export_price_max_percent", form.errors)
        data["caravan_export_price_max_percent"] = 130
        data["caravan_item_ids"] = [999999999]
        self.assertFalse(VillageCaravanConfigForm(data, instance=config).is_valid())

    def test_capacity_reduced_after_registration_still_fills_valid_load(self):
        caravan.save_demand([1049, 1056], 650)
        self.cfg.caravan_emergency_capacity = 1
        self.cfg.caravan_item_ids = [1056]
        self.cfg.save()
        self.assertEqual(self.arrive().stock, {"1056": 1})
