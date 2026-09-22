"""Small isolated regression set for player site trading (no saved world DB)."""
import copy
import uuid

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from inventory.components import COMPONENT_FIELDS
from items.models import Item
from marketplace.models import MarketListing, MarketTransaction
from player.models import PlayerState
from recipes.models import Recipe
from village.models import (EconomicLedgerEntry, VillageAccount, VillageStore, VillageStoreAccount,
    VillageStoreInventorySlot, VillageStoreListing, VillagePlayerActionReceipt, VillagePersonalCommission,
    VillagePublicDecision)
from village.player_actions import execute_player_action
from village.tests.test_player_actions import PlayerFixture


class PlayerSiteTradeTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.store, _ = VillageStore.objects.update_or_create(pk="GENERAL_STORE", defaults=dict(
            display_name="杂货店", location_code="GENERAL_STORE", operator_npc=self.npc, status="OPEN"))
        self.account, _ = VillageStoreAccount.objects.update_or_create(store=self.store, defaults=dict(balance=0))
        self.fund, _ = VillageAccount.objects.update_or_create(account_code="PUBLIC_FUND", defaults=dict(balance=0))

    def shelf(self, amount=3, price=10, item_id=1030, store=None, **batch):
        store = store or self.store
        listing = VillageStoreListing.objects.create(store=store, item_id=item_id, unit_price=price,
            listed_by_npc=self.npc, created_request_id=uuid.uuid4(), listed_world_minute=90)
        slot = VillageStoreInventorySlot.objects.create(store=store, listing=listing, item_id=item_id,
            item_amount=amount, produced_world_minute=90, expires_world_minute=1000, **batch)
        return listing, slot

    def buy(self, listing, quantity=1, **kwargs):
        return self.run_action("buy_store", dict(listing_id=str(listing.pk), quantity=quantity,
            expected_unit_price=listing.unit_price), location=listing.store.location_code, **kwargs)

    def test_store_fefo_components_fee_receipt_and_sale_history(self):
        listing, first = self.shelf(amount=1, quality=4, remaining_uses=8, ingredient="corn",
            dish_metadata={"origin": "harvest"}, rated=True, rating_price=10, overall_score=88, review_comment="good")
        second = VillageStoreInventorySlot.objects.create(store=self.store, listing=listing, item_id=1030,
            item_amount=3, produced_world_minute=95, expires_world_minute=1500, quality=2)
        payload = self.payload("buy_store", dict(listing_id=str(listing.pk), quantity=2, expected_unit_price=10), location="GENERAL_STORE")
        answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer)
        retry = {**copy.deepcopy(payload), "replay_only": True, "read_only": True, "current_location_code": ""}
        self.assertEqual(execute_player_action(retry), answer)
        self.bag_a.refresh_from_db(); self.account.refresh_from_db(); self.fund.refresh_from_db()
        self.assertEqual((self.bag_a.money, self.account.balance, self.fund.balance), (80, 19, 1))
        got = list(self.bag_a.slots.order_by("slot_index"))
        self.assertEqual([s.item_amount for s in got], [1, 1])
        for name in COMPONENT_FIELDS:
            self.assertEqual(getattr(got[0], name), getattr(first, name), name)
        self.assertEqual((got[0].quality, got[0].expires_world_minute), (4, 1000))
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual((first.item_amount, second.item_amount), (0, 2))
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="BUY_FROM_STORE").count(), 1)
        from village.phase7_services import public_store
        sale = public_store("GENERAL_STORE")["transactions"][0]
        self.assertEqual((sale["buyer_display_name"], sale["amount"]), (self.a.username, 2))

    def test_paid_restaurant_meal_and_public_subsidy_eligibility(self):
        restaurant, _ = VillageStore.objects.update_or_create(pk="RESTAURANT_STORE", defaults=dict(
            display_name="Lucy饭店", location_code="RESTAURANT", operator_npc=self.npc, status="OPEN"))
        VillageStoreAccount.objects.get_or_create(store=restaurant)
        Recipe.objects.update_or_create(pk=29201, defaults=dict(item_id=29201, recipe_name="清蒸玉米"))
        from village.lucy_cooking import fixed_price
        price = fixed_price(29201, {"dish_quality": 3})
        listing, source = self.shelf(store=restaurant, item_id=29201, price=price, ingredient="玉米", cook_time=60,
            dish_metadata={"maker_npc_id": "10014"})
        answer = self.buy(listing)
        self.assertTrue(answer["success"], answer)
        target = self.bag_a.slots.get()
        for name in COMPONENT_FIELDS:
            self.assertEqual(getattr(target, name), getattr(source, name))
        self.project()
        listing.public_meal_code = "C1"
        listing.subsidy_decision = VillagePublicDecision.objects.first()
        listing.unit_price = 0
        listing.save()
        self.fund.balance = 100; self.fund.save()
        self.assertTrue(self.buy(listing)["success"])
        self.assertEqual(self.bag_a.slots.count(), 2)
        self.assertEqual(self.buy(listing)["result_code"], "PUBLIC_MEAL_DAILY_LIMIT_REACHED")

    def test_store_daily_limit_is_per_real_player_and_last_unit(self):
        self.account.daily_limits = {"1030": 1}
        self.account.limits_until_world_minute = 1440
        self.account.save()
        listing, slot = self.shelf(amount=2)
        self.assertTrue(self.buy(listing)["success"])
        self.assertEqual(self.buy(listing)["result_code"], "STORY_RATION_LIMIT_REACHED")
        self.assertTrue(self.buy(listing, player=self.b)["success"])
        self.assertFalse(self.buy(listing, player=self.b)["success"])
        slot.refresh_from_db(); self.bag_b.refresh_from_db()
        self.assertEqual((slot.item_amount, self.bag_b.money), (0, 90))

    def test_store_invalid_site_price_stock_and_mode_do_not_mutate_assets(self):
        listing, source = self.shelf()
        for extra, code in [({"current_location_code": "RESTAURANT"}, "LOCATION_INVALID"),
                            ({"replay_only": True}, "LOCATION_REQUIRED"), ({"read_only": True}, "MODE_READ_ONLY")]:
            payload = self.payload("buy_store", dict(listing_id=str(listing.pk), quantity=1), location="GENERAL_STORE")
            payload.update(extra)
            self.assertEqual(execute_player_action(payload)["result_code"], code)
        for quantity, price, code in [(1, 99, "PRICE_CHANGED"), (4, 10, "TRADE_INSUFFICIENT_ITEM")]:
            answer = self.run_action("buy_store", dict(listing_id=str(listing.pk), quantity=quantity,
                expected_unit_price=price), location="GENERAL_STORE")
            self.assertEqual(answer["result_code"], code)
        source.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((source.item_amount, self.bag_a.money), (3, 100))
        self.assertEqual((EconomicLedgerEntry.objects.count(), VillagePersonalCommission.objects.count()), (0, 0))

    def test_market_list_delist_owner_idempotency_and_batch_preservation(self):
        source = self.slot(quantity=2, quality=5, dish_metadata={"source": "forest"})
        payload = self.payload("list_marketplace", dict(instance_id=str(source.instance_id), quantity=2, unit_price=10), location="MARKET_STREET")
        answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer)
        self.assertEqual(execute_player_action(payload), answer)
        listing_id = answer["data"]["result"]["listing_id"]
        self.assertEqual(self.run_action("delist_marketplace", dict(listing_id=listing_id), player=self.b,
            location="MARKET_STREET")["result_code"], "MARKET_DELIST_FAILED")
        payload = self.payload("delist_marketplace", dict(listing_id=listing_id), location="MARKET_STREET")
        answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer)
        self.assertEqual(execute_player_action(payload), answer)
        returned = self.bag_a.slots.get(item_amount=2)
        self.assertEqual((returned.quality, returned.expires_world_minute, returned.dish_metadata), (5, 2000, {"source": "forest"}))
        self.assertEqual(MarketListing.objects.count(), 1)

    def test_market_buy_serialized_receipt_and_last_unit(self):
        source = self.slot(quantity=1)
        listed = self.run_action("list_marketplace", dict(instance_id=str(source.instance_id), quantity=1, unit_price=10), location="MARKET_STREET")
        listing_id = listed["data"]["result"]["listing_id"]
        payload = self.payload("buy_marketplace", dict(listing_id=listing_id, quantity=1, expected_unit_price=10), player=self.b)
        answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer)
        self.assertEqual(execute_player_action(payload), answer)
        self.assertEqual(answer["data"]["result"]["transaction"]["amount"], 1)
        self.assertIn("bag", answer["data"]["result"])
        self.assertFalse(self.run_action("buy_marketplace", payload["params"], player=self.b)["success"])
        self.assertEqual(MarketTransaction.objects.count(), 1)

    def test_market_rejects_deleted_items_and_all_legacy_http_mutations(self):
        source = self.slot()
        Item.objects.filter(pk=1030).update(is_deleted=True)
        self.assertEqual(self.run_action("list_marketplace", dict(instance_id=str(source.instance_id), quantity=1,
            unit_price=10), location="MARKET_STREET")["result_code"], "MARKET_LIST_FAILED")
        client = APIClient(); client.force_authenticate(self.a)
        for endpoint in ("list", "buy", "delisting"):
            answer = client.post("/api/marketplace/" + endpoint, {"env_server": True, "current_location_code": "MARKET_STREET"}, format="json")
            self.assertEqual((answer.status_code, answer.data["result_code"]), (403, "ENV_REQUIRED"))
        with override_settings(VILLAGE_REQUIRE_ENV_MARKET_WRITES=False):
            self.assertEqual(client.post("/api/marketplace/list", {}, format="json").status_code, 400)

    def test_observer_overview_performs_no_writes(self):
        payload = self.payload("overview")
        payload["read_only"] = True
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual((VillagePlayerActionReceipt.objects.count(), PlayerState.objects.count()), (0, 0))

    def test_player_buyback_preserves_batch_balances_history_and_replay(self):
        self.account.balance = 200; self.account.save()
        source = self.slot(quantity=5, quality=4, ingredient="forest corn", rated=True,
            overall_score=80, dish_metadata={"source": "player-harvest"})
        payload = self.payload("sell_store", dict(instance_id=str(source.instance_id), quantity=2,
            expected_unit_price=5), location="GENERAL_STORE")
        answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer)
        self.assertEqual(execute_player_action({**payload, "replay_only": True, "current_location_code": ""}), answer)
        source.refresh_from_db(); self.bag_a.refresh_from_db(); self.account.refresh_from_db(); self.lucy.refresh_from_db()
        self.assertEqual((source.item_amount, self.bag_a.money, self.account.balance, self.lucy.money), (3, 110, 190, 1000))
        target = VillageStoreInventorySlot.objects.get(store=self.store)
        self.assertEqual((target.item_amount, target.quality, target.expires_world_minute, target.listing_id,
            target.source_slot_instance_id), (2, 4, 2000, None, source.instance_id))
        for name in COMPONENT_FIELDS:
            self.assertEqual(getattr(target, name), getattr(source, name), name)
        self.assertEqual(EconomicLedgerEntry.objects.filter(entry_type="SELL_TO_STORE").count(), 1)
        from village.phase7_services import public_store
        record = public_store("GENERAL_STORE")["transactions"][0]
        self.assertEqual((record["direction"], record["seller_display_name"], record["amount"], record["gross_amount"]),
            ("STORE_PURCHASE", self.a.username, 2, 10))
        quote = answer["data"]["overview"]["inventory"][0]["general_store_sale"]
        self.assertEqual((quote["allowed"], quote["unit_price"], quote["available_quantity"]), (True, 5, 3))

    def test_player_buyback_rejections_do_not_move_money_or_inventory(self):
        self.account.balance = 200; self.account.save()
        source = self.slot(quantity=3)
        params = dict(instance_id=str(source.instance_id), quantity=2, expected_unit_price=5)
        attempts = [
            (params, dict(location="MARKET_STREET"), "LOCATION_INVALID"),
            (params, dict(player=self.b), "INVALID_ITEM"),
            ({**params, "quantity": 4}, {}, "TRADE_INSUFFICIENT_ITEM"),
            ({**params, "expected_unit_price": 4}, {}, "PRICE_CHANGED"),
        ]
        for values, context, expected in attempts:
            answer = self.run_action("sell_store", values, **{"location": "GENERAL_STORE", **context})
            self.assertEqual(answer["result_code"], expected, answer)
        self.account.balance = 1; self.account.save()
        self.assertEqual(self.run_action("sell_store", params, location="GENERAL_STORE")["result_code"], "TRADE_INSUFFICIENT_MONEY")
        self.account.balance = 200; self.account.save()
        for attributes in (dict(freshness_unknown=True), dict(expires_world_minute=99)):
            for name, value in attributes.items(): setattr(source, name, value)
            source.save()
            self.assertEqual(self.run_action("sell_store", params, location="GENERAL_STORE")["result_code"], "TRADE_ITEM_NOT_FOR_SALE")
            source.freshness_unknown = False; source.expires_world_minute = 2000; source.save()
        Item.objects.filter(pk=source.item_id).update(item_type="HoeTool", max_use_count=10)
        source.remaining_uses = 9; source.save()
        self.assertEqual(self.run_action("sell_store", params, location="GENERAL_STORE")["result_code"], "TRADE_ITEM_NOT_FOR_SALE")
        source.refresh_from_db(); self.bag_a.refresh_from_db(); self.account.refresh_from_db()
        self.assertEqual((source.item_amount, self.bag_a.money, self.account.balance), (3, 100, 200))
        self.assertFalse(VillageStoreInventorySlot.objects.filter(store=self.store).exists())
        self.assertFalse(EconomicLedgerEntry.objects.filter(entry_type="SELL_TO_STORE").exists())
