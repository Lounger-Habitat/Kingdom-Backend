from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework_simplejwt.tokens import AccessToken

from account.models import GameAccount
from inventory.models import BagTemplate, NpcDefinition, NpcGlobalInventory
from inventory.services import restock_npc_bags_global
from inventory.views import (
    character_trade,
    npc_global_inventory_detail,
    npc_global_inventory_restock,
    npc_global_inventory_update,
)
from items.models import Item
from village.models import NpcProfile, VillageLocationDefinition, VillageSimulationConfig
from village.seed_data import NPC_SEEDS
from village.services import FLAG_FIELDS, validate_bootstrap


class PhaseZeroTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        for seed in NPC_SEEDS:
            for item_id, _amount in seed["items"]:
                Item.objects.get_or_create(
                    item_id=item_id,
                    defaults={
                        "item_name": f"test-item-{item_id}",
                        "item_type": "Commodity",
                        "item_price": 10,
                    },
                )
        cls.service_account = GameAccount.objects.create(
            username="phase0-env-server",
            password="unused",
        )

    def seed(self, *args):
        output = StringIO()
        call_command("seed_village_v1", *args, stdout=output)
        return output.getvalue()


class SeedVillageTests(PhaseZeroTestCase):
    def test_dry_run_rolls_back_everything(self):
        output = self.seed("--dry-run")
        self.assertIn("DRY-RUN", output)
        self.assertEqual(NpcProfile.objects.count(), 0)
        self.assertEqual(VillageLocationDefinition.objects.count(), 0)

    def test_seed_is_idempotent_and_enables_only_phase_one_runtime(self):
        self.seed()
        first_counts = (
            NpcDefinition.objects.filter(agent_id__in={row["agent_id"] for row in NPC_SEEDS}).count(),
            NpcProfile.objects.count(),
            VillageLocationDefinition.objects.count(),
            NpcGlobalInventory.objects.filter(npc__is_test_only=False).count(),
        )
        self.seed()
        self.assertEqual(
            first_counts,
            (5, 5, 16, 5),
        )
        self.assertEqual(
            first_counts,
            (
                NpcDefinition.objects.filter(agent_id__in={row["agent_id"] for row in NPC_SEEDS}).count(),
                NpcProfile.objects.count(),
                VillageLocationDefinition.objects.count(),
                NpcGlobalInventory.objects.filter(npc__is_test_only=False).count(),
            ),
        )
        self.assertEqual(NpcDefinition.objects.filter(is_test_only=True).count(), 3)
        for profile in NpcProfile.objects.filter(npc__is_test_only=False):
            self.assertTrue({"GET_RULE", "INSPECT_LIVING_OPTIONS"}.issubset(profile.capabilities))
            self.assertNotIn("WORK_PLACEHOLDER", profile.capabilities)
        config = VillageSimulationConfig.objects.get(config_key="default")
        self.assertTrue(config.village_simulation_enabled)
        self.assertTrue(config.village_hub_command_adapter_enabled)
        self.assertTrue(
            all(
                not getattr(config, field)
                for field in FLAG_FIELDS
                if field not in {
                    "village_simulation_enabled",
                    "village_hub_command_adapter_enabled",
                }
            )
        )
        self.assertTrue(
            all(
                BagTemplate.objects.filter(
                    npc__agent_id__in={row["agent_id"] for row in NPC_SEEDS}
                ).values_list("seed_only", flat=True)
            )
        )
        self.assertTrue(validate_bootstrap()["valid"])

    def test_nonempty_inventory_is_preserved_without_explicit_reset(self):
        self.seed()
        bag = NpcGlobalInventory.objects.get(npc__agent_id="10012")
        bag.money = 777
        bag.save(update_fields=["money"])
        slot = bag.slots.order_by("slot_index").first()
        slot.item_amount = 91
        slot.save(update_fields=["item_amount"])

        self.seed()
        bag.refresh_from_db()
        slot.refresh_from_db()
        self.assertEqual(bag.money, 777)
        self.assertEqual(slot.item_amount, 91)

    @override_settings(DEBUG=True, DEPLOYMENT_ENV="development")
    def test_explicit_demo_reset_restores_formal_seed_values(self):
        self.seed()
        bag = NpcGlobalInventory.objects.get(npc__agent_id="10014")
        bag.money = 1_000_000
        bag.save(update_fields=["money"])
        self.seed("--reset-demo-balances")
        bag.refresh_from_db()
        self.assertEqual(bag.money, 300)

    def test_profile_rejects_unknown_location(self):
        self.seed()
        profile = NpcProfile.objects.get(npc__agent_id="10011")
        profile.home_location_code = "MISSING_HOME"
        with self.assertRaises(ValidationError):
            profile.full_clean()


class LegacyAssetGuardTests(PhaseZeroTestCase):
    def setUp(self):
        self.seed()
        self.factory = APIRequestFactory()

    def _authenticate(self, request):
        force_authenticate(request, user=self.service_account)
        return request

    def test_two_daily_restock_runs_do_not_change_formal_npc_assets(self):
        bag = NpcGlobalInventory.objects.get(npc__agent_id="10011")
        slot = bag.slots.order_by("slot_index").first()
        slot.item_amount -= 2
        slot.save(update_fields=["item_amount"])
        before = (bag.money, slot.item_amount)

        restock_npc_bags_global()
        restock_npc_bags_global()

        bag.refresh_from_db()
        slot.refresh_from_db()
        self.assertEqual((bag.money, slot.item_amount), before)

    def test_full_inventory_update_is_rejected_with_stable_code(self):
        request = self._authenticate(
            self.factory.put("/api/npc-inventory/10011/update", {"money": 999}, format="json")
        )
        response = npc_global_inventory_update(request, "10011")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["resultCode"], "VILLAGE_NPC_LEGACY_WRITE_BLOCKED")

    def test_explicit_restock_is_rejected_with_stable_code(self):
        request = self._authenticate(
            self.factory.post(
                "/api/npc-inventory/restock",
                {"characterIds": ["10011"]},
                format="json",
            )
        )
        response = npc_global_inventory_restock(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["resultCode"], "VILLAGE_NPC_LEGACY_WRITE_BLOCKED")

    def test_missing_inventory_read_has_no_write_side_effect(self):
        NpcGlobalInventory.objects.get(npc__agent_id="10015").delete()
        request = self._authenticate(self.factory.get("/api/npc-inventory/10015"))
        response = npc_global_inventory_detail(request, "10015")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["resultCode"], "VILLAGE_NPC_NOT_BOOTSTRAPPED")
        self.assertFalse(NpcGlobalInventory.objects.filter(npc__agent_id="10015").exists())

    def test_phase_zero_trade_is_rejected_before_any_asset_mutation(self):
        request = self._authenticate(
            self.factory.post(
                "/api/env/actions/character-trade",
                {
                    "requestId": "phase0-blocked",
                    "initiator": {"type": "npc", "id": "10011"},
                    "other": {"type": "npc", "id": "10012"},
                    "giveMoney": 1,
                },
                format="json",
            )
        )
        response = character_trade(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["resultCode"], "VILLAGE_NPC_ASSET_ACTION_BLOCKED")

    def test_test_only_inventory_write_requires_explicit_flag(self):
        bag = NpcGlobalInventory.objects.get(npc__agent_id="TEST_10012")
        before = bag.money
        request = self._authenticate(
            self.factory.put(
                "/api/npc-inventory/TEST_10012/update",
                {"money": before + 1},
                format="json",
            )
        )
        response = npc_global_inventory_update(request, "TEST_10012")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["resultCode"], "VILLAGE_TEST_ASSET_ACTIONS_DISABLED")
        bag.refresh_from_db()
        self.assertEqual(bag.money, before)

    @override_settings(VILLAGE_TEST_ASSET_ACTIONS_ENABLED=True)
    def test_test_only_inventory_write_can_be_enabled_for_regression(self):
        bag = NpcGlobalInventory.objects.get(npc__agent_id="TEST_10012")
        request = self._authenticate(
            self.factory.put(
                "/api/npc-inventory/TEST_10012/update",
                {"money": 321},
                format="json",
            )
        )
        response = npc_global_inventory_update(request, "TEST_10012")
        self.assertEqual(response.status_code, 200)
        bag.refresh_from_db()
        self.assertEqual(bag.money, 321)


class VillageApiAuthenticationTests(PhaseZeroTestCase):
    def setUp(self):
        self.seed()
        self.client = APIClient()

    def _token(self, *, service):
        token = AccessToken.for_user(self.service_account)
        if service:
            token["scope"] = "env_server"
            token["aud"] = "kingdom-env"
        return str(token)

    def test_player_token_cannot_read_internal_npc_profiles(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._token(service=False)}")
        response = self.client.get("/api/village/npcs")
        self.assertEqual(response.status_code, 401)

    def test_service_token_can_read_internal_npc_profiles(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._token(service=True)}")
        response = self.client.get("/api/village/npcs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["data"]), 5)
        self.assertIn("capabilities", response.data["data"][0])
        self.assertIn("personalGoal", response.data["data"][0])
