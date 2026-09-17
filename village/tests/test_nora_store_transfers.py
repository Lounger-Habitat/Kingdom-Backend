import uuid
from types import SimpleNamespace
from rest_framework.test import APIClient
from village.tests.test_phase3_direct_trade import DirectTradeTestBase, _payload
from village.phase3_asset_service import commit_economy_action
from village import caravan
from village.models import VillageStoreInventorySlot, VillageStoreListing, EconomicLedgerEntry, NpcProfile, VillageStoryArc, VillageStoryFact


class NoraStoreTransfersTest(DirectTradeTestBase):
    def test_deposit_cannot_spend_delivery_reservation(self):
        from village.models import NpcTradeOrder, NpcTradeDelivery
        common = dict(item_id=1030, amount=5, unit_price=8,
                      delivery_point_code="MARKET_STREET", expires_world_minute=900)
        buyer = NpcTradeOrder.objects.create(request_id=uuid.uuid4(), npc=self.nora, side="BUY", **common)
        seller = NpcTradeOrder.objects.create(request_id=uuid.uuid4(), npc=self.bill, side="SELL", **common)
        NpcTradeDelivery.objects.create(buyer_order=buyer, seller_order=seller,
            seller_slot_instance_id=uuid.uuid4(), reserved_amount=5, reserved_money=40,
            delivery_point_code="MARKET_STREET", expires_world_minute=900)
        self.assertEqual(self.act("DEPOSIT_STORE_FUNDS", {"store_code":"GENERAL_STORE", "amount":411})["result_code"], "P3_INSUFFICIENT_MONEY")
        result = self.act("DEPOSIT_STORE_FUNDS", {"store_code":"GENERAL_STORE", "amount":410})
        self.assertEqual(result["result_code"], "OK")
        self.assertEqual(result["data"]["personal_available_money"], 0)
        self.nora_inv.refresh_from_db()
        self.assertEqual(self.nora_inv.money, 40)

    def act(self, name, params, actor=10015):
        return commit_economy_action(_payload(name, actor, "GENERAL_STORE", params))

    def test_deposit_preserves_total_and_replays_once(self):
        payload = _payload("DEPOSIT_STORE_FUNDS", 10015, "GENERAL_STORE", {"store_code":"GENERAL_STORE", "amount":200})
        self.assertEqual(commit_economy_action(payload)["result_code"], "OK")
        self.assertTrue(commit_economy_action(payload)["replayed"])
        self.nora_inv.refresh_from_db(); self.store_account.refresh_from_db(); self.public_fund.refresh_from_db()
        self.assertEqual((self.nora_inv.money,self.store_account.balance,self.public_fund.balance),(250,200,500))
        entry=EconomicLedgerEntry.objects.get(entry_type="DEPOSIT_STORE_FUNDS")
        self.assertEqual(entry.external_delta,0)
        self.assertEqual(self.act("WITHDRAW_STORE_FUNDS", {"store_code":"GENERAL_STORE","amount":1})["result_code"],"STORE_RESERVE_REQUIRED")

    def test_deposit_invalid_and_other_store_never_spend(self):
        for amount in (0,-1,True,1.5,451):
            with self.subTest(amount=amount):
                self.assertNotEqual(self.act("DEPOSIT_STORE_FUNDS",{"store_code":"GENERAL_STORE","amount":amount})["result_code"],"OK")
        self.assertEqual(self.act("DEPOSIT_STORE_FUNDS",{"store_code":"RESTAURANT_STORE","amount":1})["result_code"],"TRADE_STORE_FORBIDDEN")
        self.assertEqual(self.act("DEPOSIT_STORE_FUNDS",{"store_code":"GENERAL_STORE","amount":1},10014)["result_code"],"P3_FORBIDDEN_ACTOR")
        self.nora_inv.refresh_from_db(); self.assertEqual(self.nora_inv.money,450)

    def test_withdraw_excludes_listed_expired_reserved_and_is_atomic(self):
        self._give_item(self.nora_inv,1030,4)
        listing=self._list(10015,"GENERAL_STORE",1030,4,"CUSTOM",custom=8)
        good=VillageStoreInventorySlot.objects.create(store=self.store,item_id=1030,item_amount=3,reserved_amount=1,expires_world_minute=900)
        VillageStoreInventorySlot.objects.create(store=self.store,item_id=1030,item_amount=20,expires_world_minute=700)
        params={"store_code":"GENERAL_STORE","item_id":1030,"amount":3}
        self.assertEqual(self.act("WITHDRAW_STORE_ITEM",params)["result_code"],"TRADE_INSUFFICIENT_ITEM")
        good.refresh_from_db(); self.assertEqual(good.item_amount,3)
        params["amount"]=2
        payload=_payload("WITHDRAW_STORE_ITEM",10015,"GENERAL_STORE",params)
        result=commit_economy_action(payload)
        self.assertEqual(result["result_code"],"OK")
        self.assertTrue(commit_economy_action(payload)["replayed"])
        good.refresh_from_db(); self.assertEqual((good.item_amount,good.reserved_amount),(1,1))
        self.assertEqual(VillageStoreListing.objects.get(pk=listing["data"]["listing_id"]).status,"ACTIVE")
        self.assertEqual(sum(row["withdrawable_amount"] for row in caravan.store_finances(10015)["unlisted_stock"]),0)

    def test_list_delist_withdraw_keeps_quality_and_tool_uses(self):
        self._give_item(self.nora_inv,4001,2,produced=0,expires=900)
        slot=self.nora_inv.slots.get(item_id=4001)
        slot.remaining_uses=7; slot.dish_quality=5; slot.rated=True; slot.rating_price=99; slot.review_comment="original"
        slot.save()
        listing=self._list(10015,"GENERAL_STORE",4001,2,"CUSTOM",custom=50)
        self.assertEqual(self.act("DELIST_STORE_ITEM",{"listing_id":listing["data"]["listing_id"]})["result_code"],"OK")
        result=self.act("WITHDRAW_STORE_ITEM",{"store_code":"GENERAL_STORE","item_id":4001,"amount":1})
        self.assertEqual(result["result_code"],"OK")
        received=self.nora_inv.slots.get(instance_id=result["data"]["batches"][0]["personal_instance_id"])
        self.assertEqual((received.remaining_uses,received.produced_world_minute,received.expires_world_minute),(7,0,900))
        self.assertEqual((received.dish_quality,received.rated,received.rating_price,received.review_comment),(5,True,99,"original"))
        self.assertEqual(result["data"]["remaining_withdrawable"],1)
        self.assertEqual(result["data"]["personal_available_quantity"],1)

    def test_withdraw_surplus_then_export_keeps_import_stock(self):
        from village.models import VillageSimulationConfig
        VillageSimulationConfig.objects.filter(config_key="default").update(caravan_export_capacity=100)
        arc=VillageStoryArc.objects.create(arc_code="BROKEN_TRADE_ROUTE_V1",current_plan="FOREST_ROUTE",current_phase="EXECUTION")
        VillageStoryFact.objects.create(arc=arc,emergency_caravan_succeeded=True)
        visit=caravan.sync_visit(700); before=dict(visit.stock)
        VillageStoreInventorySlot.objects.create(store=self.store,item_id=1030,item_amount=110)
        self.assertEqual(self.act("WITHDRAW_STORE_ITEM",{"store_code":"GENERAL_STORE","item_id":1030,"amount":100})["result_code"],"OK")
        result=self.act("EXPORT_GOODS",{"item_id":1030,"amount":100})
        self.assertEqual(result["result_code"],"OK")
        visit.refresh_from_db(); self.store_account.refresh_from_db(); self.public_fund.refresh_from_db()
        self.assertEqual(visit.stock,before)
        self.assertEqual((self.store_account.balance,self.public_fund.balance),(0,500))

    def test_contact_explains_registration_without_route(self):
        result=self.act("CONTACT_CARAVAN",{"item_ids":[1049]})
        self.assertEqual(result["result_code"],"OK")
        self.assertTrue(result["data"]["demand_registered"])
        self.assertTrue(result["data"]["arrival_conditional"])
        self.assertFalse(result["data"]["route_open"])
        self.assertIn("FIRST_EMERGENCY_CONTACT_REQUIRED",result["data"]["missing_prerequisites"])
        self.assertNotIn("FOREST_SCOUTING_REQUIRED",result["data"]["missing_prerequisites"])

    def test_actual_discover_urls_share_specs_and_authorization(self):
        NpcProfile.objects.update_or_create(npc=self.nora,defaults={"display_name":"Nora","occupation":"merchant",
            "home_location_code":"GENERAL_STORE","work_location_code":"GENERAL_STORE","social_location_code":"MARKET_STREET",
            "capabilities":["INSPECT_INVENTORY","DEPOSIT_STORE_FUNDS","WITHDRAW_STORE_ITEM"]})
        client=APIClient()
        self.assertIn(client.get('/api/village/npcs/10015/discover').status_code,(401,403))
        client.force_authenticate(user=SimpleNamespace(is_authenticated=True))
        first=client.get('/api/village/npcs/10015/discover')
        second=client.get('/api/village/npcs/10015/available-actions')
        self.assertEqual(first.status_code,200)
        self.assertEqual(first.json(),second.json())
        self.assertIn("WITHDRAW_STORE_ITEM",[t['name'] for t in first.json()['data']['tools']])
