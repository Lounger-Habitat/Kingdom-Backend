import uuid
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from account.models import GameAccount
from village import caravan
from village.caravan_presentation import public_caravan
from village.models import VillageStoryArc, VillageStoryFact, VillageCaravanVisit, EconomicLedgerEntry
from village.phase3_asset_service import commit_economy_action
from village.tests.test_phase3_direct_trade import DirectTradeTestBase, _payload


class CaravanPresentationTests(DirectTradeTestBase):
    def setUp(self):
        super().setUp()
        arc = VillageStoryArc.objects.create(arc_code="BROKEN_TRADE_ROUTE_V1")
        self.fact = VillageStoryFact.objects.create(arc=arc, emergency_caravan_succeeded=True)
        self.time.world_minute = 700
        self.time.save()

    def visit(self, day=0, route="EMERGENCY"):
        return VillageCaravanVisit.objects.create(day_index=day, route=route,
            location_code="EMERGENCY_TRADE_POST" if route == "EMERGENCY" else "TRADE_ROAD",
            stock={"1049": 3}, initial_stock={"1049": 5}, import_prices={"1049": 7})

    def test_reading_never_creates_a_visit_and_routes_stay_separate(self):
        self.assertEqual(public_caravan("EMERGENCY")["status"], "WAITING")
        self.assertEqual(VillageCaravanVisit.objects.count(), 0)
        self.visit()
        emergency = public_caravan("EMERGENCY")
        self.assertEqual(emergency["status"], "OPEN")
        self.assertEqual(emergency["items"][0]["unit_price"], 7)
        self.assertEqual(public_caravan("NORMAL")["items"], [])
        self.fact.bridge_condition = "REPAIRED"
        self.fact.save()
        self.assertEqual(public_caravan("NORMAL")["status"], "NEXT_VISIT")
        self.assertEqual(public_caravan("EMERGENCY")["status"], "OPEN")

    def test_departed_and_sold_out_are_not_missing_data(self):
        visit = self.visit()
        visit.stock = {"1049": 0}
        visit.save()
        self.assertEqual(public_caravan("EMERGENCY")["status"], "SOLD_OUT")
        self.time.world_minute = 900
        self.time.save()
        data = public_caravan("EMERGENCY")
        self.assertEqual(data["status"], "DEPARTED")
        self.assertEqual(data["items"], [])

    def test_history_uses_frozen_route_and_includes_nora_and_legacy_store_imports(self):
        self.visit()
        self.visit(1, "NORMAL")
        for minute, payer, actor in (
            (700, "STORE:GENERAL_STORE", None), (701, "10015", self.nora),
            (702, "10011", self.bill), (703, "10015", self.bill),
            (2140, "STORE:GENERAL_STORE", None), (2141, "10015", self.nora),
        ):
            EconomicLedgerEntry.objects.create(request_id=uuid.uuid4(), entry_type="IMPORT_GOODS",
                world_minute=minute, actor_npc=actor,
                changes={"payer": payer, "nora_balance": 999, "imports": [{"item_id": 1049, "amount": 2, "unit_price": 7, "total_cost": 14}]})
        emergency = public_caravan("EMERGENCY")["history"]
        normal = public_caravan("NORMAL")["history"]
        self.assertEqual([r["world_minute"] for r in emergency], [701, 700])
        self.assertEqual([r["world_minute"] for r in normal], [2141, 2140])
        self.assertNotIn("nora_balance", str(emergency))
        self.assertEqual(emergency[0]["total_cost"], 14)
        self.assertEqual((emergency[0]["payment_source"], emergency[0]["stock_destination"]),
                         ("PERSONAL", "PERSONAL_INVENTORY"))
        self.assertEqual((emergency[1]["payment_source"], emergency[1]["stock_destination"]),
                         ("STORE", "STORE_UNLISTED"))

    def test_successful_personal_import_appears_in_history_after_real_settlement(self):
        self.time.world_minute = 650
        self.time.save()
        demand = commit_economy_action(_payload(
            "CONTACT_CARAVAN", 10015, "GENERAL_STORE", {"item_ids": [1049]},
        ))
        self.assertEqual(demand["result_code"], "OK")
        visit = caravan.sync_visit(660)
        self.time.world_minute = 700
        self.time.save()
        imported = commit_economy_action(_payload(
            "IMPORT_GOODS", 10015, "EMERGENCY_TRADE_POST", {"item_id": 1049, "amount": 2},
        ))
        self.assertEqual(imported["result_code"], "OK")
        entry = EconomicLedgerEntry.objects.get(entry_type="IMPORT_GOODS")
        self.assertEqual(entry.changes["payer"], "10015")
        result = public_caravan("EMERGENCY")
        self.assertEqual(result["history_total"], 1)
        self.assertEqual(result["history"], [{
            "ledger_id": entry.pk, "world_minute": 700, "item_id": 1049,
            "amount": 2, "unit_price": visit.import_prices["1049"],
            "total_cost": imported["data"]["total_cost"], "operator_display_name": "Nora",
            "payment_source": "PERSONAL", "stock_destination": "PERSONAL_INVENTORY", "item_name": "盐",
        }])
        self.assertNotIn("nora_balance", str(result))
        self.assertEqual(public_caravan("NORMAL")["history"], [])

    def test_history_pages_reach_every_import_across_days_without_splitting_receipts(self):
        for day in range(3):
            self.visit(day)
            for index in range(12):
                EconomicLedgerEntry.objects.create(
                    request_id=uuid.uuid4(), entry_type="IMPORT_GOODS", world_minute=day * 1440 + 700 + index,
                    actor_npc=self.nora if index % 2 else None,
                    changes={"payer": "10015" if index % 2 else "STORE:GENERAL_STORE", "imports": [
                        {"item_id": 1049, "amount": 2, "unit_price": 7, "total_cost": 14},
                        {"item_id": 4001, "amount": 1, "unit_price": 65, "total_cost": 65},
                    ]})
        self.time.world_minute = 3 * 1440 + 700
        self.time.save()
        first = public_caravan("EMERGENCY")
        second = public_caravan("EMERGENCY", history_page=2)
        self.assertEqual((first["history_page"], first["history_pages"], first["history_total"]), (1, 2, 36))
        self.assertEqual(len(first["history"]), 60)
        self.assertEqual(len(second["history"]), 12)
        self.assertEqual(second["history"][-1]["world_minute"], 700)
        first_ids = {row["ledger_id"] for row in first["history"]}
        second_ids = {row["ledger_id"] for row in second["history"]}
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(len(first_ids | second_ids), 36)
        self.assertEqual(public_caravan("EMERGENCY", history_page=99)["history_page"], 2)

    def test_endpoint_requires_player_auth_and_feature_flag(self):
        from village.models import VillageSimulationConfig
        client = APIClient()
        path = "/api/village/public/caravans/EMERGENCY"
        self.assertIn(client.get(path).status_code, (401, 403))
        user = GameAccount.objects.create(username="caravan-viewer", password="unused")
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
        VillageSimulationConfig.objects.filter(config_key="default").update(village_presentation_enabled=True)
        self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(client.get(path + "?history_page=2").status_code, 200)
        for page in ("0", "-1", "abc", "1.5"):
            self.assertEqual(client.get(path + "?history_page=" + page).status_code, 400)
        self.assertEqual(client.get(path + "?private=true").status_code, 400)
        self.assertEqual(client.get("/api/village/public/caravans/UNKNOWN").status_code, 404)
        self.assertEqual(client.post(path, {}).status_code, 405)
        VillageSimulationConfig.objects.filter(config_key="default").update(village_presentation_enabled=False)
        self.assertEqual(client.get(path).status_code, 404)
