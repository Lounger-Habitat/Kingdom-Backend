import uuid

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from account.models import GameAccount
from inventory.models import NpcDefinition
from village.models import NpcProfile, VillageActionReceipt


class QuickInputReceiptTests(TestCase):
    def setUp(self):
        npc, _ = NpcDefinition.objects.update_or_create(
            agent_id="10011", defaults={"name": "Bill", "is_test_only": False},
        )
        NpcProfile.objects.update_or_create(npc=npc, defaults={
            "display_name": "Bill", "occupation": "farmer", "is_active": True,
            "home_location_code": "HOME", "work_location_code": "FARM", "social_location_code": "SQUARE",
        })
        self.env = GameAccount.objects.create(username="quick-input-env", password="unused")
        self.player = GameAccount.objects.create(username="quick-input-player", password="unused")

    def client_for(self, account, *, env=False):
        token = AccessToken.for_user(account)
        if env:
            token["scope"] = "env_server"
            token["aud"] = "kingdom-env"
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def body(self, request_id):
        return {
            "request_id": str(request_id), "npc_id": "10011", "action": "SPEECH",
            "success": True, "result_code": "DIALOGUE_SPOKEN", "world_minute": 0, "time_revision": 1,
            "params": {
                "conversation_id": "quick-input-conversation", "text": "我想租块田。",
                "speech_scope": "player_dialogue", "speaker_role": "player", "player_id": str(self.player.pk),
                "input_metadata": {"source": "quick_preset", "quick_input_id": "bill-rent", "edited": False},
            },
        }

    def test_source_survives_existing_success_receipt_and_replay_does_not_rewrite_it(self):
        request_id = uuid.uuid4()
        client = self.client_for(self.env, env=True)
        body = self.body(request_id)
        response = client.post("/api/village/runtime/instruction-outcome", body, format="json")
        self.assertEqual((response.status_code, response.data["resultCode"]), (200, "RECORDED"))
        body["params"]["input_metadata"]["source"] = "voice"
        response = client.post("/api/village/runtime/instruction-outcome", body, format="json")
        self.assertEqual((response.status_code, response.data["resultCode"]), (200, "REPLAYED"))
        receipt = VillageActionReceipt.objects.get(request_id=request_id)
        params = receipt.response_data["parameters"]
        self.assertEqual(params["player_id"], str(self.player.pk))
        self.assertEqual(params["input_metadata"], {
            "source": "quick_preset", "quick_input_id": "bill-rent", "edited": False,
        })

    def test_player_cannot_write_an_authoritative_history_receipt(self):
        request_id = uuid.uuid4()
        response = self.client_for(self.player).post(
            "/api/village/runtime/instruction-outcome", self.body(request_id), format="json",
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(VillageActionReceipt.objects.filter(request_id=request_id).exists())
