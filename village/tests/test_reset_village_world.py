from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory
from items.models import Item
from village.models import (
    NpcActionReservation,
    NpcDialogueSession,
    NpcRelationship,
    NpcRuntimeState,
    NpcSubjectiveGoalState,
    VillageAccount,
    VillageDeliveryOutbox,
    VillageSimulationConfig,
    VillageStoryArc,
    VillageWorldEvent,
)
from village.seed_data import INITIAL_SUPPLEMENTARY_GRANTS, NPC_SEEDS, TOOL_ITEM_SEEDS


@override_settings(DEBUG=True, DEPLOYMENT_ENV="development")
class ResetVillageWorldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        required = {item_id for seed in NPC_SEEDS for item_id, _ in seed["items"]}
        required.update(item_id for _, item_id, _ in INITIAL_SUPPLEMENTARY_GRANTS)
        for item_id in required:
            Item.objects.get_or_create(
                item_id=item_id,
                defaults={"item_name": f"item-{item_id}", "item_type": "Commodity", "item_price": 10},
            )
        for seed in TOOL_ITEM_SEEDS:
            Item.objects.update_or_create(
                item_id=seed["item_id"],
                defaults={
                    "item_name": seed["item_name"],
                    "item_type": seed["item_type"],
                    "item_description": seed["item_description"],
                    "item_price": seed["item_price"],
                    "collectable": seed["collectable"],
                },
            )
        call_command("seed_village_v1", stdout=StringIO())

    def test_reset_restores_world_and_preserves_feature_flags(self):
        config = VillageSimulationConfig.objects.get(config_key="default")
        config.village_needs_enabled = True
        config.village_economy_enabled = True
        config.village_social_enabled = True
        config.village_story_enabled = True
        config.save()

        bill_bag = NpcGlobalInventory.objects.get(npc__agent_id="10011")
        bill_bag.money = 999
        bill_bag.asset_version = 7
        bill_bag.save()
        bill_runtime = NpcRuntimeState.objects.get(npc__agent_id="10011")
        bill_runtime.current_location_code = "GENERAL_STORE"
        bill_runtime.state_version = 9
        bill_runtime.save()
        NpcSubjectiveGoalState.objects.create(npc=bill_runtime.npc, summary="旧目标", status="ACTIVE")
        reservation = NpcActionReservation.objects.create(
            action_kind="DIALOGUE", acquired_world_minute=0, expires_world_minute=1
        )
        NpcDialogueSession.objects.create(
            conversation_id="reset-dialogue",
            initiator=bill_runtime.npc,
            target=NpcRuntimeState.objects.get(npc__agent_id="10012").npc,
            reservation=reservation,
            started_world_minute=0,
            expires_world_minute=1,
        )
        VillageWorldEvent.objects.create(
            dedupe_key="reset-test-event",
            event_type="TEST",
            world_minute=1,
            time_revision=1,
        )
        VillageAccount.objects.update(balance=123, asset_version=4)
        Item.objects.filter(item_id=4003).delete()

        call_command(
            "reset_village_world",
            "--confirm",
            "RESET_WUXI_VILLAGE",
            stdout=StringIO(),
        )

        time_state = GlobalGameTimeState.objects.get(pk=1)
        self.assertEqual(
            (time_state.game_year, time_state.game_month, time_state.game_day,
             time_state.game_hour, time_state.game_minute, time_state.time_revision),
            (2026, 3, 8, 7, 0, 0),
        )
        bill_bag.refresh_from_db()
        bill_runtime = NpcRuntimeState.objects.get(npc__agent_id="10011")
        config.refresh_from_db()
        self.assertEqual((bill_bag.money, bill_bag.asset_version), (200, 0))
        self.assertEqual(
            (bill_runtime.current_location_code, bill_runtime.action_status, bill_runtime.state_version),
            ("BILL_HOME", "IDLE", 0),
        )
        self.assertEqual(
            dict(NpcRuntimeState.objects.values_list("npc__agent_id", "current_location_code")),
            {
                "10011": "BILL_HOME",
                "10012": "JACK_HOME",
                "10013": "RECORD_HALL",
                "10014": "RESTAURANT",
                "10015": "GENERAL_STORE",
            },
        )
        self.assertFalse(NpcSubjectiveGoalState.objects.exists())
        self.assertFalse(NpcDialogueSession.objects.exists())
        reset_event = VillageWorldEvent.objects.get(event_type="VILLAGE_WORLD_RESET")
        self.assertEqual(reset_event.public_data["story_id"], "BROKEN_TRADE_ROUTE_V1")
        self.assertTrue(VillageDeliveryOutbox.objects.filter(event=reset_event).exists())
        self.assertFalse(VillageStoryArc.objects.exists())
        self.assertEqual(NpcRelationship.objects.count(), 20)
        nora_bag = NpcGlobalInventory.objects.get(npc__agent_id="10015")
        self.assertEqual(
            dict(nora_bag.slots.filter(item_id__in=(4001, 4002)).values_list("item_id", "item_amount")),
            {4001: 3, 4002: 3},
        )
        self.assertTrue(config.village_needs_enabled)
        self.assertTrue(config.village_economy_enabled)
        self.assertTrue(config.village_social_enabled)
        self.assertTrue(config.village_story_enabled)

    def test_confirmation_and_dry_run_are_safe(self):
        with self.assertRaises(CommandError):
            call_command("reset_village_world", stdout=StringIO())

        bag = NpcGlobalInventory.objects.get(npc__agent_id="10011")
        bag.money = 777
        bag.save(update_fields=["money"])
        call_command("reset_village_world", "--dry-run", stdout=StringIO())
        bag.refresh_from_db()
        self.assertEqual(bag.money, 777)

    def test_restore_npc_needs_only_updates_the_five_runtime_rows(self):
        bill = NpcRuntimeState.objects.get(npc__agent_id="10011")
        bill.current_location_code = "GENERAL_STORE"
        bill.hunger_fp = 999
        bill.energy_fp = 1
        bill.health_fp = 2
        bill.social_fp = 998
        bill.stress_fp = 997
        bill.state_version = 7
        bill.save()

        call_command("restore_npc_needs", stdout=StringIO())

        bill.refresh_from_db()
        clock = GlobalGameTimeState.get_instance()
        self.assertEqual(
            (bill.hunger_fp, bill.energy_fp, bill.health_fp, bill.social_fp, bill.stress_fp),
            (0, 1000, 1000, 0, 0),
        )
        self.assertEqual((bill.current_location_code, bill.state_version), ("GENERAL_STORE", 8))
        self.assertEqual((bill.needs_world_minute, bill.time_revision), (clock.world_minute, clock.time_revision))
