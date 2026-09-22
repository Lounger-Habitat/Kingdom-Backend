import uuid
from types import SimpleNamespace

from django.test import SimpleTestCase

from village.phase4_visibility import ENV_CLIENTS, event_payload


class GiftFeedbackProjectionTests(SimpleTestCase):
    def event(self):
        return SimpleNamespace(
            event_id=uuid.uuid4(), event_sequence=4, event_type="ECONOMY_GIVE_ITEMS",
            world_minute=700, time_revision=700, actor_npc=SimpleNamespace(agent_id="10011"),
            participant_npc_ids=["10011"], witness_npc_ids=[], player_ids=["42"],
            location_code="FARM", point_code="", visibility="PARTICIPANTS",
            public_data={}, player_summary="Gift delivered", hub_summary="Gift delivered",
            command_id=None, request_id=uuid.uuid4(), cause_event_id=None,
            private_data={"status": "DELIVERED", "target_type": "PLAYER", "target_id": "42",
                          "sender_npc_id": "10011", "sender_name": "比尔", "exchange_id": "private",
                          "items": [{"item_id": 1030, "item_name": "小麦", "amount": 5,
                                     "source_instance_id": "private-source", "received_instance_id": "private-target"}]},
        )

    def test_only_confirmed_recipient_receipt_is_projected_without_private_batches(self):
        event = self.event()
        projected = event_payload(event, ENV_CLIENTS)
        gift = projected["gift"]
        self.assertEqual(gift["gift_id"], str(event.event_id))
        self.assertEqual(gift["player_id"], "42")
        self.assertEqual(gift["items"], [{"item_id": 1030, "item_name": "小麦", "amount": 5}])
        self.assertNotIn("private", str(projected))
        self.assertNotIn("gift", event_payload(event, "10012"))

    def test_public_unsettled_and_wrong_recipient_events_have_no_receipt(self):
        for field, value in (("status", "PENDING"), ("target_type", "NPC"), ("target_id", "43")):
            event = self.event()
            event.private_data[field] = value
            self.assertNotIn("gift", event_payload(event, ENV_CLIENTS))
        event = self.event()
        event.visibility = "PUBLIC"
        self.assertNotIn("gift", event_payload(event, ENV_CLIENTS))

    def test_gold_only_receipt_is_projected(self):
        event = self.event()
        event.private_data.update(items=[], gold_amount=16)
        gift = event_payload(event, ENV_CLIENTS)["gift"]
        self.assertEqual(gift["gold_amount"], 16)
        self.assertEqual(gift["items"], [])
        self.assertNotIn("gift", event_payload(event, "10012"))
