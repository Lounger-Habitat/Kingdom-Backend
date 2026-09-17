"""Daily visit, funding and ownership regressions through real settlement."""
import uuid

from village import caravan
from village.models import (
    VillageCaravanVisit, VillageCaravanRequest, VillageStoryArc, VillageStoryFact,
    VillageSimulationConfig, VillageStoreInventorySlot, VillageStoreListing,
    NpcCommandState, NpcRuntimeState, EconomicLedgerEntry,
)
from village.phase3_asset_service import commit_economy_action
from village.phase5_story import get_store_purchase_limit
from village.tests.test_phase3_direct_trade import DirectTradeTestBase, _payload


class DailyCaravanTest(DirectTradeTestBase):
    def setUp(self):
        super().setUp()
        self.cfg = VillageSimulationConfig.objects.get(config_key="default")
        self.cfg.caravan_item_ids = [1049, 4001]
        self.cfg.save()
        self.arc = VillageStoryArc.objects.create(arc_code="BROKEN_TRADE_ROUTE_V1", current_phase="EXECUTION")
        self.fact = VillageStoryFact.objects.create(arc=self.arc, emergency_caravan_succeeded=True)
        self.clock(650)

    def clock(self, minute):
        self.time.world_minute = minute
        self.time.time_revision = minute
        self.time.save()

    def act(self, name, params, actor=10015):
        return commit_economy_action(_payload(name, actor, "GENERAL_STORE", params))

    def arrive(self):
        self.clock(660)
        return caravan.sync_visit(660)

    def test_queries_do_not_generate_stock(self):
        self.clock(700)
        caravan.projection(700)
        self.assertEqual(VillageCaravanVisit.objects.count(), 0)

    def test_demand_once_capacity_and_no_reroll(self):
        result = self.act("CONTACT_CARAVAN", {"item_ids": [1049, 4001]})
        self.assertEqual(result["result_code"], "OK")
        self.assertEqual(result["data"]["arrival_world_minute"], 660)
        visit = self.arrive()
        self.assertEqual(visit.location_code, "EMERGENCY_TRADE_POST")
        self.assertEqual(set(visit.stock), {"1049", "4001"})
        self.assertEqual(sum(visit.stock.values()), 20)
        self.assertTrue(all(q >= 1 for q in visit.stock.values()))
        self.assertEqual(caravan.sync_visit(700).stock, visit.stock)
        result = self.act("CONTACT_CARAVAN", {"item_ids": [1049]})
        self.assertEqual(result["data"]["arrival_world_minute"], 2100)
        self.assertEqual(caravan.sync_visit(700).stock, visit.stock)
        self.clock(2100)
        tomorrow = caravan.sync_visit(2100)
        self.assertEqual(set(tomorrow.stock), {"1049"})
        self.clock(3540)
        following = caravan.sync_visit(3540)
        self.assertEqual(following.requested_items, [])
        self.assertTrue(following.stock)

    def test_bad_demand_keeps_previous_request(self):
        self.assertEqual(self.act("CONTACT_CARAVAN", {"item_ids": [1049]})["result_code"], "OK")
        for items in ([], [True], [1030], [999999]):
            self.assertNotEqual(self.act("CONTACT_CARAVAN", {"item_ids": items})["result_code"], "OK")
        self.assertEqual(VillageCaravanRequest.objects.get(pk=0).item_ids, [1049])
        self.cfg.caravan_emergency_capacity = 1
        self.cfg.save()
        self.assertEqual(self.act("CONTACT_CARAVAN", {"item_ids": [1049, 4001]})["result_code"], "CARAVAN_CAPACITY_EXCEEDED")

    def test_repair_changes_next_visit_only_and_route_survives_story_end(self):
        today = self.arrive()
        self.fact.bridge_condition = "REPAIRED"
        self.fact.save()
        self.arc.is_completed = True
        self.arc.save()
        self.assertEqual(caravan.sync_visit(800).pk, today.pk)
        self.assertEqual(caravan.sync_visit(800).route, "EMERGENCY")
        self.clock(2100)
        self.assertEqual(caravan.sync_visit(2100).route, "NORMAL")

    def test_route_unlocked_after_arrival_waits_till_next_day(self):
        self.fact.emergency_caravan_succeeded = False
        self.fact.save()
        self.assertEqual(self.arrive().route, "NONE")
        self.fact.emergency_caravan_succeeded = True
        self.fact.save()
        self.assertEqual(caravan.sync_visit(800).route, "NONE")
        self.clock(2100)
        self.assertEqual(caravan.sync_visit(2100).route, "EMERGENCY")

    def test_open_close_and_latest_start(self):
        self.assertEqual(caravan.trade_window(659), "CARAVAN_CLOSED")
        self.arrive()
        self.assertIsNone(caravan.trade_window(660, 10))
        self.assertIsNone(caravan.trade_window(890, 10))
        self.assertEqual(caravan.trade_window(891, 10), "CARAVAN_TOO_LATE")
        self.assertIsNone(caravan.trade_window(900, completing=True))
        self.assertEqual(caravan.trade_window(900), "CARAVAN_CLOSED")
        self.clock(900)
        self.assertEqual(self.act("IMPORT_GOODS", {"item_id": 1049, "amount": 1})["result_code"], "CARAVAN_CLOSED")

    def test_purchase_remaining_stock_and_replay(self):
        caravan.save_demand([1049], 650)
        visit = self.arrive()
        before = visit.stock["1049"]
        payload = _payload("IMPORT_GOODS", 10015, "EMERGENCY_TRADE_POST", {"item_id": 1049, "amount": 1})
        first = commit_economy_action(payload)
        self.assertEqual(first["result_code"], "OK")
        self.assertTrue(commit_economy_action(payload)["replayed"])
        visit.refresh_from_db()
        self.assertEqual(visit.stock["1049"], before - 1)
        self.assertEqual(self.act("IMPORT_GOODS", {"item_id": 1049, "amount": before})["result_code"], "CARAVAN_INSUFFICIENT_STOCK")
        visit.refresh_from_db()
        self.assertEqual(visit.stock["1049"], before - 1)

    def test_export_ignores_incoming_capacity_and_pays_personal(self):
        self.cfg.caravan_export_capacity = 120
        self.cfg.save()
        visit = self.arrive()
        visit.stock = {"1049": 0}
        visit.save()
        self._give_item(self.nora_inv, 1030, 100)
        self._give_item(self.bill_inv, 1030, 10)
        result = self.act("EXPORT_GOODS", {"item_id": 1030, "amount": 100})
        self.assertEqual(result["result_code"], "OK")
        self.nora_inv.refresh_from_db()
        self.public_fund.refresh_from_db()
        self.assertEqual(self.nora_inv.money, 450 + 100 * visit.export_prices["1030"])
        self.assertEqual(self.public_fund.balance, 500)
        self.assertTrue(caravan.projection(660)["can_export"])

    def test_expired_items_and_store_safety_stock(self):
        self.arrive()
        self._give_item(self.nora_inv, 1030, 8)
        self._give_item(self.bill_inv, 1030, 20, expires=660)
        self.assertEqual(self.act("EXPORT_GOODS", {"item_id": 1030, "amount": 8})["result_code"], "EXPORT_SAFETY_STOCK_REQUIRED")
        VillageStoreInventorySlot.objects.create(store=self.store, item_id=1030, item_amount=10)
        self.assertEqual(self.act("EXPORT_GOODS", {"item_id": 1030, "amount": 8})["result_code"], "OK")

    def test_personal_import_list_delist_and_withdraw(self):
        caravan.save_demand([1049], 650)
        self.arrive()
        self.store_account.balance = 500
        self.store_account.save()
        payload = _payload("IMPORT_GOODS", 10015, "EMERGENCY_TRADE_POST", {"item_id": 1049, "amount": 1})
        imported = commit_economy_action(payload)
        self.assertEqual(imported["result_code"], "OK")
        self.assertEqual(imported["data"]["payer"], "10015")
        self.assertEqual(imported["data"]["stock_destination"], "PERSONAL_INVENTORY")
        self.assertTrue(commit_economy_action(payload)["replayed"])
        self.nora_inv.refresh_from_db()
        self.store_account.refresh_from_db()
        self.assertEqual(self.nora_inv.money, 450 - imported["data"]["total_cost"])
        self.assertEqual(self.store_account.balance, 500)
        self.assertEqual(sum(self.nora_inv.slots.filter(item_id=1049).values_list("item_amount", flat=True)), 1)
        self.assertFalse(VillageStoreInventorySlot.objects.filter(store=self.store, item_id=1049).exists())
        self.assertFalse(VillageStoreListing.objects.filter(store=self.store, item_id=1049).exists())
        listing = self.act("LIST_STORE_ITEM", {"store_code": "GENERAL_STORE", "item_id": 1049, "amount": 1,
            "stock_source": "PERSONAL", "price_mode": "CUSTOM", "custom_unit_price": 10})
        self.assertEqual(listing["result_code"], "OK")
        self.assertEqual(self.act("DELIST_STORE_ITEM", {"listing_id": listing["data"]["listing_id"]})["result_code"], "OK")
        self.assertEqual(caravan.store_finances(10015)["unlisted_stock"][0]["item_amount"], 1)
        self.store_account.refresh_from_db()
        available = self.store_account.balance - 200
        self.assertEqual(self.act("WITHDRAW_STORE_FUNDS", {"store_code": "GENERAL_STORE", "amount": available + 1})["result_code"], "STORE_RESERVE_REQUIRED")
        payload = _payload("WITHDRAW_STORE_FUNDS", 10015, "GENERAL_STORE", {"store_code": "GENERAL_STORE", "amount": available})
        self.assertEqual(commit_economy_action(payload)["result_code"], "OK")
        self.assertTrue(commit_economy_action(payload)["replayed"])
        self.store_account.refresh_from_db()
        self.assertEqual(self.store_account.balance, 200)

    def test_import_cannot_fall_back_to_store_money(self):
        caravan.save_demand([1049], 650)
        visit = self.arrive()
        stock_before = dict(visit.stock)
        self.nora_inv.money = 0
        self.nora_inv.save()
        self.store_account.balance = 500
        self.store_account.save()
        result = self.act("IMPORT_GOODS", {"item_id": 1049, "amount": 1})
        self.assertEqual(result["result_code"], "P3_INSUFFICIENT_MONEY")
        self.assertEqual(result["data"]["payer"], "10015")
        self.assertEqual(result["data"]["available_amount"], 0)
        rejected = self.act("IMPORT_GOODS", {"item_id": 1049, "amount": 1, "store_code": "GENERAL_STORE"})
        self.assertEqual(rejected["result_code"], "P3_INVALID_PARAMS")
        self.nora_inv.refresh_from_db()
        self.store_account.refresh_from_db()
        visit.refresh_from_db()
        self.assertEqual((self.nora_inv.money, self.store_account.balance), (0, 500))
        self.assertEqual(visit.stock, stock_before)
        self.assertFalse(self.nora_inv.slots.filter(item_id=1049, item_amount__gt=0).exists())
        self.assertFalse(VillageStoreInventorySlot.objects.filter(store=self.store, item_id=1049).exists())

    def test_store_buys_from_villager_without_spending_personal_money(self):
        self.store_account.balance = 50  # below withdrawal floor: purchases still work
        self.store_account.save()
        self._give_item(self.bill_inv, 1030, 4)
        proposal = self.act("PROPOSE_TRADE", {"target_npc_id": "10011", "side": "BUY", "item_id": 1030,
            "amount": 4, "gold_amount": 30, "store_code": "GENERAL_STORE", "expires_world_minute": 900})
        self.assertEqual(proposal["result_code"], "OK")
        accepted = self.act("RESPOND_TRADE", {"offer_id": proposal["data"]["offer_id"], "decision": "ACCEPT"}, actor=10011)
        self.assertEqual(accepted["result_code"], "OK")
        self.store_account.refresh_from_db()
        self.nora_inv.refresh_from_db()
        self.bill_inv.refresh_from_db()
        self.assertEqual((self.store_account.balance, self.nora_inv.money, self.bill_inv.money), (20, 450, 230))
        self.assertEqual(caravan.store_finances(10015)["unlisted_stock"][0]["item_amount"], 4)

    def test_store_limits_expire_and_cannot_clear_public_ration(self):
        self.cfg.village_story_enabled = True
        self.cfg.save()
        self.fact.store_limits_enabled = True
        self.fact.ration_daily_limits = {"1030": 3}
        self.fact.save()
        self.arc.is_completed = True
        self.arc.save()
        result = self.act("SET_STORE_LIMITS", {"store_code": "GENERAL_STORE", "item_id": 1030,
            "daily_limit": 2, "expires_world_minute": 800})
        self.assertEqual(result["result_code"], "OK")
        self.assertEqual(get_store_purchase_limit(10011, 1030, 650), 2)
        self.assertEqual(get_store_purchase_limit(10011, 1030, 800), 3)
        self.act("SET_STORE_LIMITS", {"store_code": "GENERAL_STORE", "item_id": 1030,
            "daily_limit": 0, "expires_world_minute": 900})
        self.assertEqual(get_store_purchase_limit(10011, 1030, 650), 3)

    def test_recorded_finish_at_departure_settles_after_clock_catchup(self):
        caravan.save_demand([1049], 650)
        self.arrive()
        self.clock(905)
        payload = _payload("IMPORT_GOODS", 10015, "EMERGENCY_TRADE_POST", {"item_id": 1049, "amount": 1})
        command = NpcCommandState.objects.create(command_id=payload["command_id"], request_id=payload["request_id"],
            npc=self.nora, message_id=uuid.uuid4(), kind="IMPORT_GOODS", status="RUNNING", started_world_minute=890, duration_minutes=10,
            sanitized_parameters=payload["params"])
        NpcRuntimeState.objects.filter(npc=self.nora).update(active_command_id=command.command_id, needs_world_minute=905)
        self.assertEqual(commit_economy_action(payload)["result_code"], "OK")

    def test_no_partial_export_on_safety_failure(self):
        self.arrive()
        self._give_item(self.nora_inv, 1030, 15)
        result = self.act("EXPORT_GOODS", {"item_id": 1030, "amount": 8})
        self.assertEqual(result["result_code"], "EXPORT_SAFETY_STOCK_REQUIRED")
        self.assertEqual(sum(self.nora_inv.slots.filter(item_id=1030).values_list("item_amount", flat=True)), 15)
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="EXPORT_GOODS").count(), 0)

    def test_public_report_keeps_personal_export_out_of_public_fund(self):
        from village.phase3_public_economy import _public_fund_money_delta
        from village.phase7_services import _fund_delta
        self.arrive()
        self._give_item(self.nora_inv, 1030, 30)
        self.assertEqual(self.act("EXPORT_GOODS", {"item_id": 1030, "amount": 20})["result_code"], "OK")
        entry = EconomicLedgerEntry.objects.get(entry_type="EXPORT_GOODS")
        self.assertEqual(_public_fund_money_delta(entry), 0)
        self.assertEqual(_fund_delta(entry), 0)
        entry.changes = {"export": {"amount": 20}}  # legacy exports retain their history
        self.assertEqual(_fund_delta(entry), entry.external_delta)

    def test_daily_limit_covers_multiple_listings_and_query_reports_remainder(self):
        from village.phase3_direct_trade import query_store_inventory
        self._give_item(self.nora_inv, 1030, 10)
        first = self._list(10015, "GENERAL_STORE", 1030, 5, "CUSTOM", custom=5)
        second = self._list(10015, "GENERAL_STORE", 1030, 5, "CUSTOM", custom=6)
        self.assertEqual(self.act("SET_STORE_LIMITS", {"store_code": "GENERAL_STORE", "item_id": 1030,
            "daily_limit": 2, "expires_world_minute": 3000})["result_code"], "OK")
        self.assertEqual(self._buy(10011, first["data"]["listing_id"], 2)["result_code"], "OK")
        self.assertEqual(self._buy(10011, second["data"]["listing_id"], 1)["result_code"], "STORY_RATION_LIMIT_REACHED")
        _, data = query_store_inventory("GENERAL_STORE", 650, "10011")
        self.assertEqual(data["purchase_limits"]["remaining_today"]["1030"], 0)
        self.clock(1500)
        self.assertEqual(self._buy(10011, second["data"]["listing_id"], 1)["result_code"], "OK")

    def test_public_ration_expires_even_after_story_end(self):
        self.cfg.village_story_enabled = True
        self.cfg.save()
        self.fact.store_limits_enabled = True
        self.fact.ration_daily_limits = {"1030": 1}
        self.fact.ration_limits_until_world_minute = 800
        self.fact.save()
        self.arc.is_completed = True
        self.arc.save()
        self.assertEqual(get_store_purchase_limit(10011, 1030, 799), 1)
        self.assertIsNone(get_store_purchase_limit(10011, 1030, 800))

    def test_start_rejects_too_late_before_any_asset_settlement(self):
        from village.runtime_services import CommandService
        self.arrive()
        self.clock(891)
        command = NpcCommandState.objects.create(npc=self.nora, message_id=uuid.uuid4(), request_id=uuid.uuid4(),
            kind="IMPORT_GOODS", status="RECEIVED", duration_minutes=10,
            sanitized_parameters={"item_id": 1049, "amount": 1})
        accepted, code, _ = CommandService.start_command(command.command_id)
        self.assertFalse(accepted)
        self.assertEqual(code, "CARAVAN_TOO_LATE")
        self.nora_inv.refresh_from_db()
        self.assertEqual(self.nora_inv.money, 450)
