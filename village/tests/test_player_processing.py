"""In-memory physical processing, shared Jack tool and fresh-batch boundaries."""
import uuid
from unittest.mock import patch

from django.test import TestCase
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot, InventoryBag, InventorySlot
from inventory.views import get_or_create_bag
from rest_framework.exceptions import ValidationError
from player.models import PlayerGrowthState, SkillRewardReceipt
from recipes.models import Recipe
from village.models import (NpcRuntimeState, VillageActionDurationConfig,
                            VillagePlayerProcessingJob, EconomicLedgerEntry)
from village.phase3_catalog import PREY_CHICKEN, PREY_DEER, VENISON
from village.phase3_asset_service import get_available_slot_amount
from village.player_processing import (execute_processing_action, processing_overview,
    settle_processing_jobs, ProcessingError, jack_processing_busy)
from village.tests.test_player_actions import PlayerFixture


class PlayerProcessingTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        jack, _ = NpcDefinition.objects.get_or_create(agent_id="10012", defaults=dict(name="Jack"))
        self.jack, _ = NpcGlobalInventory.objects.update_or_create(npc=jack, defaults=dict(money=240))
        self.jack.slots.all().delete()
        from items.models import Item
        from village.phase3_catalog import PREY_RECIPES
        for item_id in {*PREY_RECIPES, *(i for spec in PREY_RECIPES.values() for i in spec["outputs"]), 4002}:
            Item.objects.get_or_create(pk=item_id, defaults=dict(item_name=str(item_id), item_price=10))
        self.rt, _ = NpcRuntimeState.objects.update_or_create(npc=jack, defaults=dict(
            current_location_code="BUTCHER_YARD", health_fp=1000, energy_fp=800,
            hunger_fp=200, needs_world_minute=100, active_command_id=None, action_status="IDLE"))
        self.tool = NpcGlobalInventorySlot.objects.create(bag=self.jack, slot_index=0,
            item_id=4002, item_amount=1, remaining_uses=20)
        VillageActionDurationConfig.objects.update_or_create(action_name="PROCESS_GAME", defaults=dict(duration_minutes=5))

    def call(self, action, params, *, player=None, bag=None):
        return execute_processing_action(player or self.a, bag or self.bag_a, action, params, uuid.uuid4(), self.clock)

    def start(self, slot=None, *, player=None, bag=None):
        slot = slot or self.slot(item_id=PREY_CHICKEN, quantity=1, quality=3)
        params = dict(prey_instance_id=str(slot.instance_id))
        preview = self.call("preview_game_processing", params, player=player, bag=bag)["preview"]
        self.assertTrue(preview["available"], preview)
        return self.call("start_game_processing", dict(**params, expected_quote=preview["quote_hash"]), player=player, bag=bag)["job"]

    def advance(self, minute):
        self.clock.world_minute = minute
        self.clock.save()
        return settle_processing_jobs(minute)

    def test_preview_and_overview_are_zero_write_and_catalog_has_two_deer_soups(self):
        prey = self.slot(item_id=PREY_DEER, quantity=1, quality=3)
        before = (InventoryBag.objects.count(), InventorySlot.objects.count(), EconomicLedgerEntry.objects.count(), PlayerGrowthState.objects.count())
        first = processing_overview(self.a, self.bag_a, 100)
        self.assertTrue(first["inputs"][0]["can_start"])
        self.call("preview_game_processing", dict(prey_instance_id=str(prey.instance_id)))
        self.assertEqual(before, (InventoryBag.objects.count(), InventorySlot.objects.count(), EconomicLedgerEntry.objects.count(), PlayerGrowthState.objects.count()))
        self.assertEqual(set(Recipe.objects.filter(pk__in=[29216, 29226]).values_list("pk", flat=True)), {29216, 29226})
        from village.player_cooking_rules import VILLAGE_RECIPES
        self.assertEqual(VILLAGE_RECIPES[29216], (4, 2))
        self.assertEqual(VILLAGE_RECIPES[29226], (4, 2))

    def test_real_escrow_cancel_preserves_entire_original_and_fee(self):
        prey = self.slot(item_id=PREY_CHICKEN, quantity=2, quality=4, source_request_id=uuid.uuid4())
        record = self.start(prey)
        job = VillagePlayerProcessingJob.objects.get(pk=record["job_id"])
        prey.refresh_from_db(); self.bag_a.refresh_from_db(); self.jack.refresh_from_db()
        self.assertEqual((prey.item_amount, self.bag_a.money, job.escrow_bag.money, self.jack.money), (1, 98, 2, 240))
        with self.assertRaises(ValidationError):
            get_or_create_bag(self.a, job.escrow_bag.character_id)
        self.call("cancel_game_processing", dict(job_id=record["job_id"]))
        job.refresh_from_db(); job.input_slot.refresh_from_db(); self.bag_a.refresh_from_db(); self.tool.refresh_from_db()
        self.assertEqual((job.status, self.bag_a.money, self.tool.remaining_uses), ("CANCELLED", 100, 20))
        self.assertEqual(job.input_slot.bag_id, self.bag_a.pk)
        self.assertEqual((job.input_slot.quality, job.input_slot.produced_world_minute, job.input_slot.expires_world_minute, job.input_slot.source_request_id),
            (prey.quality, prey.produced_world_minute, prey.expires_world_minute, prey.source_request_id))

    def test_shared_busy_and_real_tool_reservation(self):
        job = self.start()
        self.assertTrue(jack_processing_busy("10012"))
        self.assertFalse(jack_processing_busy("10014"))
        self.assertEqual(get_available_slot_amount(self.tool, 100), 0)
        prey = self.slot(item_id=PREY_CHICKEN, quantity=1, bag=self.bag_b)
        preview = self.call("preview_game_processing", dict(prey_instance_id=str(prey.instance_id)), player=self.b, bag=self.bag_b)["preview"]
        self.assertEqual(preview["reason"], "NPC_BUSY")
        self.call("cancel_game_processing", dict(job_id=job["job_id"]))
        self.rt.active_command_id = uuid.uuid4(); self.rt.save()
        preview = self.call("preview_game_processing", dict(prey_instance_id=str(prey.instance_id)), player=self.b, bag=self.bag_b)["preview"]
        self.assertEqual(preview["reason"], "NPC_BUSY")

    def test_completion_pays_and_wears_once_late_claim_does_not_refresh(self):
        prey = self.slot(item_id=PREY_DEER, quantity=1, quality=4, expires_world_minute=120)
        row = self.start(prey)
        self.assertEqual(self.advance(105), [self.a.pk])
        self.assertEqual(self.advance(105), [])
        self.jack.refresh_from_db(); self.tool.refresh_from_db()
        self.assertEqual((self.jack.money, self.tool.remaining_uses), (245, 19))
        job = VillagePlayerProcessingJob.objects.get(pk=row["job_id"])
        self.assertEqual(job.status, "READY")
        rev = PlayerGrowthState.objects.get(player=self.a).revision
        self.advance(130)
        self.assertEqual(PlayerGrowthState.objects.get(player=self.a).revision, rev)
        self.call("claim_game_processing", dict(job_id=row["job_id"]))
        meat = self.bag_a.slots.get(item_id=VENISON, item_amount=4)
        self.assertEqual((meat.quality, meat.produced_world_minute, meat.expires_world_minute), (4, 90, 120))
        self.assertFalse(meat.dish_metadata)
        self.assertEqual(self.bag_a.slots.get(item_id=29105, item_amount=1).expires_world_minute, None)
        self.assertFalse(SkillRewardReceipt.objects.filter(player=self.a).exists())
        with self.assertRaises(ProcessingError):
            self.call("claim_game_processing", dict(job_id=row["job_id"]))
        self.jack.refresh_from_db(); self.tool.refresh_from_db()
        self.assertEqual((self.jack.money, self.tool.remaining_uses), (245, 19))

    def test_tool_failure_is_recoverable_without_swallowing_assets(self):
        row = self.start()
        self.tool.remaining_uses = 0; self.tool.save()
        self.assertEqual(self.advance(105), [self.a.pk])
        job = VillagePlayerProcessingJob.objects.get(pk=row["job_id"])
        self.assertEqual(job.status, "FAILED")
        self.assertEqual(job.escrow_bag.money, 2)
        self.assertEqual(job.input_slot.item_amount, 1)
        self.jack.refresh_from_db(); self.assertEqual(self.jack.money, 240)
        self.call("cancel_game_processing", dict(job_id=row["job_id"]))
        self.bag_a.refresh_from_db(); self.assertEqual(self.bag_a.money, 100)

    def test_boundary_freshness_quote_and_owner_gate(self):
        prey = self.slot(item_id=PREY_CHICKEN, quantity=1, expires_world_minute=105)
        preview = self.call("preview_game_processing", dict(prey_instance_id=str(prey.instance_id)))["preview"]
        self.assertEqual(preview["reason"], "PREY_TOO_SHORT_LIVED")
        prey.expires_world_minute = 106; prey.save()
        quote = self.call("preview_game_processing", dict(prey_instance_id=str(prey.instance_id)))["preview"]
        self.tool.remaining_uses = 19; self.tool.save()
        with self.assertRaises(ProcessingError) as caught:
            self.call("start_game_processing", dict(prey_instance_id=str(prey.instance_id), expected_quote=quote["quote_hash"]))
        self.assertEqual(caught.exception.code, "QUOTE_CHANGED")
        row = self.start(prey)
        with self.assertRaises(ProcessingError) as caught:
            self.call("cancel_game_processing", dict(job_id=row["job_id"]), player=self.b, bag=self.bag_b)
        self.assertEqual(caught.exception.code, "PROCESSING_JOB_NOT_OWNED")

    def test_completion_output_failure_rolls_back_tool_fee_and_input(self):
        row = self.start()
        original = InventorySlot.objects.create
        def reject_output(**kwargs):
            if kwargs["item_id"] == 29104:
                raise ValueError("INJECTED_FAILURE")
            return original(**kwargs)
        with patch.object(InventorySlot.objects, "create", side_effect=reject_output):
            self.advance(105)
        job = VillagePlayerProcessingJob.objects.get(pk=row["job_id"])
        self.assertEqual((job.status, job.input_slot.item_amount, job.escrow_bag.money), ("FAILED", 1, 2))
        self.tool.refresh_from_db(); self.jack.refresh_from_db()
        self.assertEqual((self.tool.remaining_uses, self.jack.money), (20, 240))
        self.assertEqual(job.escrow_bag.slots.filter(item_amount__gt=0).count(), 1)
