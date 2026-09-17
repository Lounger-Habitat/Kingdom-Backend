from django.test import TestCase
from village.models import VillageDeliveryOutbox, VillageWorldEvent
from village.phase3_asset_service import commit_economy_action
from village.tests import test_lucy_restaurant as restaurant_tests
from village.tests.test_phase3_production import _payload


class LucyRuntimeRepairsTests(TestCase):
    # Reuse fixture helpers without inheriting/rerunning the full restaurant suite.
    setUp = restaurant_tests.LucyRestaurantTests.setUp
    act = restaurant_tests.LucyRestaurantTests.act
    cook = restaurant_tests.LucyRestaurantTests.cook
    listing = restaurant_tests.LucyRestaurantTests.listing
    fund_with = restaurant_tests.LucyRestaurantTests.fund_with
    decision = restaurant_tests.LucyRestaurantTests.decision
    def test_sale_notifies_only_operator_and_replay_does_not_notify_twice(self):
        listing = self.listing(self.cook(), 2)
        payload = _payload("BUY_FROM_STORE", 10011, "RESTAURANT", {"listing_id": listing, "amount": 1})
        result = commit_economy_action(payload)
        self.assertEqual(result["result_code"], "OK", result)
        event = VillageWorldEvent.objects.get(request_id=payload["request_id"])
        delivery = VillageDeliveryOutbox.objects.get(event=event, message_kind="EVENT")
        self.assertEqual(delivery.recipient, "10014")
        sale = delivery.payload["public_data"]["private_data"]
        self.assertEqual((sale["store_code"], sale["amount"], sale["remaining_amount"]), ("RESTAURANT_STORE", 1, 1))
        self.assertEqual(sale["store_net_amount"], result["data"]["store_net_amount"])
        self.assertNotIn("purchased_batches", sale)
        replay = commit_economy_action(payload)
        self.assertTrue(replay["replayed"])
        self.assertEqual(VillageDeliveryOutbox.objects.filter(event=event, message_kind="EVENT").count(), 1)

    def test_public_meal_sale_notifies_subsidy_without_charging_customer(self):
        self.fund_with(12)
        decision = self.decision()
        listing = self.listing(self.cook(29211), public_meal_code="C2", decision_id=str(decision.pk))
        result = self.act("BUY_FROM_STORE", {"listing_id": listing, "amount": 1}, npc="10011")
        event = VillageWorldEvent.objects.get(request_id=result["request_id"])
        sale = VillageDeliveryOutbox.objects.get(event=event, message_kind="EVENT").payload["public_data"]["private_data"]
        self.assertEqual((sale["gross_amount"], sale["subsidy_amount"], sale["store_net_amount"]), (0, 12, 12))
        self.bags["10011"].refresh_from_db()
        self.assertEqual(self.bags["10011"].money, 100)

    def test_invalid_recipe_and_cancelled_trade_are_explicit_without_asset_changes(self):
        before = list(self.lucy.slots.values_list("instance_id", "item_amount"))
        failure = self.act("COOK", {"recipe_id": 3002}, expect="P3_RECIPE_NOT_ALLOWED")
        self.assertEqual(failure["data"]["invalid_field"], "recipe_id")
        self.assertEqual(before, list(self.lucy.slots.values_list("instance_id", "item_amount")))
        offer = self.act("PROPOSE_TRADE", {"side": "SELL", "target_npc_id": "10012",
            "item_id": 1056, "amount": 1, "gold_amount": 12, "expires_world_minute": 3000})["data"]["offer_id"]
        self.act("CANCEL_TRADE_OFFER", {"offer_id": offer})
        result = self.act("RESPOND_TRADE", {"offer_id": offer, "decision": "ACCEPT"},
                          npc="10012", expect="TRADE_OFFER_ALREADY_RESOLVED")
        self.assertEqual(result["data"]["offer_status"], "CANCELLED")
        self.assertEqual(result["data"]["cancelled_by_npc_id"], "10014")
        self.lucy.refresh_from_db()
        self.assertEqual(self.lucy.money, 100)
