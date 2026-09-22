"""Player/NPC public meals share cash, stock and ten daily portions."""
import copy
import uuid
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase
from account.models import GameAccount
from inventory.models import InventoryBag, NpcDefinition, NpcGlobalInventory
from items.models import Item
from village.models import (EconomicLedgerEntry, VillageAccount, VillageCommunityMealDailyUsage,
    VillagePublicDecision, VillageStore, VillageStoreAccount, VillageStoreInventorySlot, VillageStoreListing)
from village.lucy_catalog import PUBLIC_MEALS
from village.lucy_restaurant import buy_public_meal
from village.player_actions import execute_player_action
from village.tests.test_player_actions import PlayerFixture


class PlayerPublicMealTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.store, _ = VillageStore.objects.update_or_create(pk="RESTAURANT_STORE", defaults=dict(
            display_name="Lucy饭店", location_code="RESTAURANT", operator_npc=self.npc, status="OPEN"))
        self.account, _ = VillageStoreAccount.objects.update_or_create(store=self.store, defaults=dict(balance=0))
        self.fund, _ = VillageAccount.objects.update_or_create(account_code="PUBLIC_FUND", defaults=dict(balance=200))

    def shelf(self, code="C1", amount=20):
        spec = PUBLIC_MEALS[code]
        Item.objects.get_or_create(pk=spec["item_id"], defaults=dict(item_name=code, item_type="食物", item_price=10))
        listing = VillageStoreListing.objects.create(store=self.store, item_id=spec["item_id"], unit_price=0,
            public_meal_code=code, listed_by_npc=self.npc, created_request_id=uuid.uuid4(), listed_world_minute=90)
        slot = VillageStoreInventorySlot.objects.create(store=self.store, listing=listing, item_id=listing.item_id,
            item_amount=amount, produced_world_minute=90, expires_world_minute=3000, quality=4,
            ingredient="玉米", cook_time=60, dish_metadata={"maker_npc_id": "10014", "recipe_id": 29201})
        return listing, slot

    def claim(self, listing, **kwargs):
        return self.run_action("buy_store", dict(listing_id=str(listing.pk), quantity=1, expected_unit_price=0),
            location="RESTAURANT", **kwargs)

    def npc_claim(self, listing, npc_id):
        npc, _ = NpcDefinition.objects.get_or_create(agent_id=npc_id, defaults=dict(name=npc_id))
        NpcGlobalInventory.objects.get_or_create(npc=npc)
        with transaction.atomic():
            return buy_public_meal(npc_id, dict(listing_id=str(listing.pk), amount=1), self.clock.world_minute, uuid.uuid4())

    def test_no_authorization_real_batch_single_subsidy_receipt_and_private_quota(self):
        listing, source = self.shelf("C2")
        self.bag_a.money = 0; self.bag_a.save()
        payload = self.payload("buy_store", dict(listing_id=str(listing.pk), quantity=1, expected_unit_price=0), location="RESTAURANT")
        answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer)
        retry = {**copy.deepcopy(payload), "replay_only": True, "read_only": True, "current_location_code": ""}
        self.assertEqual(execute_player_action(retry), answer)
        self.fund.refresh_from_db(); self.account.refresh_from_db(); source.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((self.fund.balance, self.account.balance, source.item_amount, self.bag_a.money), (188, 12, 19, 0))
        target = self.bag_a.slots.get()
        self.assertEqual((target.quality, target.ingredient, target.cook_time, target.expires_world_minute), (4, "玉米", 60, 3000))
        marker = target.dish_metadata["public_meal_claim"]
        self.assertEqual((marker["actor_type"], marker["actor_id"]), ("PLAYER", str(self.a.pk)))
        self.assertFalse(VillagePublicDecision.objects.exists())
        self.assertEqual(EconomicLedgerEntry.objects.count(), 1)
        ledger = EconomicLedgerEntry.objects.get()
        self.assertEqual(ledger.changes["PUBLIC_FUND"]["money"], -12)
        quota = answer["data"]["overview"]["public_meals"]
        self.assertEqual((quota["remaining_personal_today"], quota["remaining_village_today"]), (0, 9))
        self.assertNotIn("player_usage", quota)
        from village.phase7_services import public_store
        self.assertEqual(public_store(self.store.pk)["transactions"][0]["subsidy_amount"], 12)

    def test_all_menus_share_personal_quota_and_reset_on_game_day_not_real_day(self):
        listings = [self.shelf(code)[0] for code in ("C1", "C2", "R1")]
        self.assertTrue(self.claim(listings[0])["success"])
        for listing in listings:
            self.assertEqual(self.claim(listing)["result_code"], "PUBLIC_MEAL_DAILY_LIMIT_REACHED")
        self.clock.world_minute = 1440; self.clock.save()
        self.assertTrue(self.claim(listings[2])["success"])
        self.assertEqual(list(VillageCommunityMealDailyUsage.objects.order_by("day_index").values_list("total_meals", flat=True)), [1, 1])
        self.assertEqual(self.claim(listings[1])["result_code"], "PUBLIC_MEAL_DAILY_LIMIT_REACHED")

    def test_five_npcs_and_five_players_share_ten_portions_including_last_one(self):
        listing, slot = self.shelf()
        for npc in ("10011", "10012", "10013", "10014", "10015"):
            answer = self.npc_claim(listing, npc)
            self.assertEqual(answer["result_code"], "OK", answer)
        for index in range(5):
            player = GameAccount.objects.create(username=f"diner-{index}")
            InventoryBag.objects.create(player=player, character_id=player.username)
            self.assertTrue(self.claim(listing, player=player)["success"])
        self.assertEqual(self.claim(listing)["result_code"], "PUBLIC_MEAL_DAILY_LIMIT_REACHED")
        usage = VillageCommunityMealDailyUsage.objects.get()
        self.assertEqual((usage.total_meals, sum(usage.npc_usage.values()), sum(usage.player_usage.values())), (10, 5, 5))
        slot.refresh_from_db(); self.fund.refresh_from_db(); self.account.refresh_from_db()
        self.assertEqual((slot.item_amount, self.fund.balance, self.account.balance), (10, 110, 90))

    def test_last_portion_claimed_by_player_blocks_new_npc(self):
        listing, _ = self.shelf()
        VillageCommunityMealDailyUsage.objects.create(day_index=0, total_meals=9, player_usage={str(i): 1 for i in range(100, 109)})
        self.assertTrue(self.claim(listing)["success"])
        self.assertEqual(self.npc_claim(listing, "10011")["result_code"], "PUBLIC_MEAL_DAILY_LIMIT_REACHED")

    def test_equal_player_and_npc_numeric_ids_have_independent_allowance(self):
        player = GameAccount.objects.create(pk=10011, username="same-number-as-bill")
        InventoryBag.objects.create(player=player, character_id=player.username)
        listing, _ = self.shelf()
        self.assertEqual(self.npc_claim(listing, "10011")["result_code"], "OK")
        self.assertTrue(self.claim(listing, player=player)["success"])
        usage = VillageCommunityMealDailyUsage.objects.get()
        self.assertEqual((usage.npc_usage, usage.player_usage), ({"10011": 1}, {"10011": 1}))

    def test_expired_reserved_marked_or_unfunded_food_cannot_charge_or_use_quota(self):
        listing, slot = self.shelf()
        for change, expected in [({"expires_world_minute": 100}, "TRADE_INSUFFICIENT_ITEM"),
                                 ({"reserved_amount": 20}, "TRADE_INSUFFICIENT_ITEM"),
                                 ({"dish_metadata": {"maker_npc_id": "10014", "public_meal_claim": {"actor_id": "old"}}}, "PUBLIC_MEAL_INVALID_BATCH")]:
            for field, value in change.items(): setattr(slot, field, value)
            slot.save()
            self.assertEqual(self.claim(listing)["result_code"], expected)
            slot.expires_world_minute = 3000; slot.reserved_amount = 0
            slot.dish_metadata = {"maker_npc_id": "10014"}; slot.save()
        self.fund.balance = 8; self.fund.save()
        self.assertEqual(self.claim(listing)["result_code"], "PUBLIC_MEAL_FUND_INSUFFICIENT")
        slot.refresh_from_db(); self.account.refresh_from_db()
        self.assertEqual((slot.item_amount, self.account.balance), (20, 0))
        self.assertFalse(VillageCommunityMealDailyUsage.objects.exists())
        self.assertFalse(self.bag_a.slots.exists())
        self.assertFalse(EconomicLedgerEntry.objects.exists())

    def test_one_portion_only_trusted_location_and_no_client_selected_recipient(self):
        listing, slot = self.shelf()
        base = dict(listing_id=str(listing.pk), quantity=1, expected_unit_price=0)
        for params, location, expected in [({**base, "quantity": 2}, "RESTAURANT", "PUBLIC_MEAL_ONE_PORTION_ONLY"),
                (base, "GENERAL_STORE", "LOCATION_INVALID"), ({**base, "player_id": str(self.b.pk)}, "RESTAURANT", "INVALID_PARAMS")]:
            self.assertEqual(self.run_action("buy_store", params, location=location)["result_code"], expected)
        self.assertFalse(VillageCommunityMealDailyUsage.objects.exists())
        slot.refresh_from_db(); self.assertEqual(slot.item_amount, 20)

    def test_failure_after_settlement_rolls_back_stock_money_and_quota(self):
        from village.player_actions import ActionError
        listing, slot = self.shelf()
        with patch("village.player_store.InventorySlot.save", side_effect=ActionError("TEST_FAILURE", "rollback")):
            self.assertEqual(self.claim(listing)["result_code"], "TEST_FAILURE")
        slot.refresh_from_db(); self.fund.refresh_from_db(); self.account.refresh_from_db()
        self.assertEqual((slot.item_amount, self.fund.balance, self.account.balance), (20, 200, 0))
        self.assertFalse(VillageCommunityMealDailyUsage.objects.exists())
        self.assertTrue(self.claim(listing)["success"])
