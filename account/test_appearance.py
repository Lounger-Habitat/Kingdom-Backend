import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .appearance import CATALOG, SELECTABLE
from .models import GameAccount
from .views import _jwt_response


class PlayerAppearanceTests(TestCase):
    def setUp(self):
        self.account = GameAccount.objects.create(username="appearance-one", password="unused")
        self.other = GameAccount.objects.create(username="appearance-two", password="unused")
        self.client = APIClient()
        self.client.force_authenticate(self.account)

    def save(self, model="artisan_male_01", revision=0, **extra):
        return self.client.put("/api/player/me/appearance", {
            "appearance": {"schemaVersion": 1, "modelId": model}, "expectedRevision": revision, **extra
        }, format="json")

    def test_first_save_restores_and_does_not_touch_other_account(self):
        self.assertIsNone(self.client.get("/api/player/me/appearance").json()["data"]["appearance"])
        self.assertEqual(self.save().status_code, 200)
        self.client.force_authenticate(GameAccount.objects.get(pk=self.account.pk))
        profile = self.client.get("/api/player/me/appearance").json()["data"]
        self.assertEqual(profile["appearance"]["modelId"], "artisan_male_01")
        self.assertEqual(profile["appearanceRevision"], 1)
        self.other.refresh_from_db()
        self.assertIsNone(self.other.appearance)

    def test_repeat_is_idempotent_but_saved_appearance_cannot_change(self):
        self.save()
        self.assertEqual(self.save().json()["data"]["appearanceRevision"], 1)
        stale = self.save("artisan_female_01", 0)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.save("artisan_female_01", 1).status_code, 409)
        self.assertEqual(self.save("artisan_male_01", 1).json()["data"]["appearanceRevision"], 1)
        self.account.refresh_from_db()
        self.assertEqual(self.account.appearance["modelId"], "artisan_male_01")

    def test_reserved_unknown_paths_and_identity_override_are_rejected(self):
        for model in [e["id"] for e in CATALOG["models"] if e["npcOnly"]] + ["unknown", "../Prefabs/RemotePlayer"]:
            self.assertEqual(self.save(model).status_code, 400, model)
        self.assertEqual(self.save(accountId=self.other.pk).status_code, 400)

    def test_invalid_shape_schema_and_revision_are_rejected(self):
        for body in [[], {"appearance": None, "expectedRevision": 0},
                     {"appearance": {"schemaVersion": True, "modelId": "artisan_male_01"}, "expectedRevision": 0},
                     {"appearance": {"schemaVersion": 2, "modelId": "artisan_male_01"}, "expectedRevision": 0},
                     {"appearance": {"schemaVersion": 1, "modelId": "artisan_male_01", "hairId": "x"}, "expectedRevision": 0}]:
            self.assertEqual(self.client.put("/api/player/me/appearance", body, format="json").status_code, 400)
        for revision in [-1, True, "0", 2147483647]:
            self.assertEqual(self.save(revision=revision).status_code, 400)

    def test_authentication_is_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/player/me/appearance").status_code, 401)
        self.assertEqual(self.save().status_code, 401)

    def test_auth_and_refresh_return_saved_profile(self):
        self.save(); self.account.refresh_from_db()
        self.assertEqual(_jwt_response(self.account)["data"]["appearanceRevision"], 1)
        response = self.client.post("/api/auth/refresh", {"refresh": str(RefreshToken.for_user(self.account))}, format="json")
        self.assertEqual(response.json()["data"]["appearance"], self.account.appearance)

    def test_guest_conversion_keeps_profile(self):
        self.account.is_guest = True; self.account.save()
        self.save()
        self.account.refresh_from_db()
        self.client.force_authenticate(self.account)
        response = self.client.post("/api/auth/convert", {"new_username": "appearance_member", "new_password": "test-pass-123"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["appearanceRevision"], 1)
        self.assertEqual(response.json()["data"]["user_id"], self.account.pk)

    def test_unity_and_backend_catalog_match_and_reserve_exact_npc_meshes(self):
        unity_path = Path(__file__).resolve().parents[2] / "Assets/Resources/PlayerAppearanceCatalog.json"
        self.assertEqual(CATALOG, json.loads(unity_path.read_text(encoding="utf-8")))
        self.assertEqual(len(SELECTABLE), 6)
        self.assertEqual({e["meshName"] for e in CATALOG["models"] if e["npcOnly"]}, {
            "SM_Chr_Peasant_Male_01", "SM_Chr_Merchant_01", "SM_Chr_Hermit_01",
            "SM_Chr_Peasant_Female_01", "SM_Chr_Rider_01"})

    @patch("account.views.validate_player_access_token")
    def test_env_verification_uses_backend_profile(self, verify):
        self.save(); self.account.refresh_from_db(); verify.return_value = self.account
        response = self.client.post("/api/env/auth/verify-player", {"playerAccessToken": "test"}, format="json")
        self.assertEqual(response.json()["data"]["appearanceRevision"], 1)
