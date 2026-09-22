import uuid
from django.test import TestCase
from rest_framework.test import APIClient
from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot, NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item
from village.models import VillageSimulationConfig, NpcRuntimeState, VillageWorldEvent, VillageDeliveryOutbox


class NpcDialogueGiftTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.player = GameAccount.objects.create(username="npc-gift-test", password="unused")
        self.client.force_authenticate(self.player)
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=100)
        self.npc, _ = NpcDefinition.objects.get_or_create(agent_id="10011", defaults={"name": "Bill"})
        self.npc_bag, _ = NpcGlobalInventory.objects.get_or_create(npc=self.npc, defaults={"money": 20})
        self.npc_bag.money = 20
        self.npc_bag.save()
        NpcRuntimeState.objects.get_or_create(npc=self.npc)
        config, _ = VillageSimulationConfig.objects.get_or_create(config_key="default")
        config.village_economy_enabled = config.village_social_enabled = config.village_direct_trade_enabled = True
        config.save()
        clock = GlobalGameTimeState.get_instance()
        clock.world_minute = 1000
        clock.save()
        Item.objects.get_or_create(item_id=98765, defaults={"item_name": "Gift test item"})
        self.slot = InventorySlot.objects.create(bag=self.bag, slot_index=0, item_id=98765, item_amount=5, quality=4)
        self.body = {
            "requestId": str(uuid.uuid4()), "requesterId": str(self.player.pk),
            "initiator": {"type": "player", "id": str(self.player.pk)},
            "other": {"type": "npc", "id": "10011"},
            "giveItem": [{"instanceId": str(self.slot.instance_id), "itemId": 98765, "amount": 2}],
            "giveMoney": 15, "getItem": [], "getMoney": 0,
            "giftContext": "npc_dialogue", "conversationId": "test-meeting",
        }

    def post(self):
        return self.client.post("/api/env/actions/character-trade", self.body, format="json")

    def test_mixed_gift_is_atomic_idempotent_and_preserves_batch_quality(self):
        first = self.post()
        self.assertEqual(first.status_code, 200, first.content)
        second = self.post()
        self.assertEqual(first.json(), second.json())
        self.slot.refresh_from_db(); self.bag.refresh_from_db(); self.npc_bag.refresh_from_db()
        self.assertEqual((self.slot.item_amount, self.bag.money, self.npc_bag.money), (3, 85, 35))
        transferred = NpcGlobalInventorySlot.objects.get(bag=self.npc_bag, item_id=98765)
        self.assertEqual((transferred.item_amount, transferred.quality), (2, 4))
        receipt = self.client.post("/api/env/actions/character-trade/receipt", {"requestId": self.body["requestId"], "requesterId": str(self.player.pk)}, format="json")
        self.assertEqual(receipt.json()["receipt"], first.json())

    def test_gold_only_gift(self):
        self.body["giveItem"] = []
        self.assertEqual(self.post().status_code, 200)
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 85)
        facts = VillageDeliveryOutbox.objects.get(event__event_type="PLAYER_GIFT").payload["public_data"]["private_data"]
        self.assertEqual((facts["items"], facts["gold"]), ([], 15))

    def test_gift_notifies_only_recipient_with_sender_and_delivered_assets_once(self):
        NpcRuntimeState.objects.filter(npc=self.npc).update(current_location_code="MARKET_STREET")
        witness, _ = NpcDefinition.objects.get_or_create(agent_id="10013", defaults={"name": "Mark"})
        NpcRuntimeState.objects.update_or_create(npc=witness, defaults={"current_location_code": "MARKET_STREET"})
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 200)
        event = VillageWorldEvent.objects.get(event_type="PLAYER_GIFT")
        self.assertEqual(event.visibility, "PARTICIPANTS")
        self.assertEqual(event.witness_npc_ids, [])
        deliveries = list(VillageDeliveryOutbox.objects.filter(event=event))
        self.assertEqual([row.recipient for row in deliveries], ["10011"])
        content = deliveries[0].payload
        facts = content["public_data"]["private_data"]
        self.assertEqual(facts, {
            "npc_id": "10011", "player_id": str(self.player.pk), "player_name": self.player.username,
            "items": [{"item_id": 98765, "item_name": "Gift test item", "amount": 2}], "gold": 15,
            "request_id": self.body["requestId"], "conversation_id": "test-meeting",
        })
        for text in (self.player.username, "Gift test item×2", "15 金币", "已到账"):
            self.assertIn(text, content["summary"])

    def test_item_only_gift_reports_full_batch_after_source_slot_is_cleared(self):
        self.body["giveItem"][0]["amount"] = 5
        self.body["giveMoney"] = 0
        self.assertEqual(self.post().status_code, 200)
        facts = VillageDeliveryOutbox.objects.get(event__event_type="PLAYER_GIFT").payload["public_data"]["private_data"]
        self.assertEqual(facts["items"], [{"item_id": 98765, "item_name": "Gift test item", "amount": 5}])
        self.assertEqual(facts["gold"], 0)

    def test_rescue_gift_also_notifies_only_recipient_with_gold_details(self):
        del self.body["giftContext"]
        self.body["giveItem"] = []
        self.assertEqual(self.post().status_code, 200)
        event = VillageWorldEvent.objects.get(event_type="PLAYER_RESCUE_GIFT")
        deliveries = list(VillageDeliveryOutbox.objects.filter(event=event))
        self.assertEqual([row.recipient for row in deliveries], ["10011"])
        facts = deliveries[0].payload["public_data"]["private_data"]
        self.assertEqual((facts["items"], facts["gold"], facts["player_id"]), ([], 15, str(self.player.pk)))

    def test_insufficient_money_does_not_transfer_items(self):
        self.body["giveMoney"] = 101
        self.assertEqual(self.post().status_code, 400)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.item_amount, 5)
        self.assertFalse(NpcGlobalInventorySlot.objects.filter(bag=self.npc_bag, item_id=98765).exists())
        self.assertFalse(VillageWorldEvent.objects.filter(event_type="PLAYER_GIFT").exists())

    def test_duplicate_batch_cannot_overdraw(self):
        self.body["giveItem"] *= 3
        self.assertEqual(self.post().status_code, 400)
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.money, 100)

    def test_expired_batch_cannot_be_gifted(self):
        self.slot.expires_world_minute = 999
        self.slot.save()
        self.assertEqual(self.post().status_code, 400)

    def test_legacy_rescue_restriction_remains(self):
        del self.body["giftContext"]
        self.assertEqual(self.post().status_code, 409)

    def test_no_reverse_assets_and_explicit_batch_required(self):
        self.body["getMoney"] = 1
        self.assertEqual(self.post().status_code, 409)
        self.body["getMoney"] = 0
        del self.body["giveItem"][0]["instanceId"]
        self.assertEqual(self.post().status_code, 400)

    def test_receipt_is_scoped_to_account(self):
        self.assertEqual(self.post().status_code, 200)
        other = GameAccount.objects.create(username="other-gift-test", password="unused")
        receipt = self.client.post("/api/env/actions/character-trade/receipt", {"requestId": self.body["requestId"], "requesterId": str(other.pk)}, format="json")
        self.assertFalse(receipt.json()["found"])

    def test_old_receipt_returns_current_inventory_separately(self):
        self.assertEqual(self.post().status_code, 200)
        InventoryBag.objects.filter(pk=self.bag.pk).update(money=42)
        receipt = self.client.post("/api/env/actions/character-trade/receipt", {"requestId": self.body["requestId"], "requesterId": str(self.player.pk)}, format="json")
        self.assertEqual(receipt.json()["receipt"]["data"]["initiatorInventory"]["money"], 85)
        self.assertEqual(receipt.json()["currentInventory"]["money"], 42)

    def test_player_jwt_cannot_attest_a_dialogue_gift(self):
        from rest_framework_simplejwt.tokens import AccessToken
        self.client.force_authenticate(user=None)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + str(AccessToken.for_user(self.player)))
        self.assertEqual(self.post().status_code, 401)
