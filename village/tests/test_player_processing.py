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
    def test_batch_submission_escrows_each_carcass_and_fifo_work_is_independent(self):
        prey = self.slot(item_id=PREY_CHICKEN, quantity=4, quality=4, source_request_id=uuid.uuid4())
        params = dict(prey_instance_id=str(prey.instance_id), quantity=3)
        quote = self.call("preview_game_processing", params)["preview"]
        self.assertEqual((quote["quantity"], quote["fee_gold"], quote["unit_fee_gold"], quote["total_duration_minutes"]), (3, 6, 2, 15))
        self.assertEqual([row["quantity"] for row in quote["outputs"]], [9, 3])
        result = self.call("start_game_processing", dict(params, expected_quote=quote["quote_hash"]))
        jobs = list(VillagePlayerProcessingJob.objects.order_by("submitted_at", "job_id"))
        self.assertEqual(len(result["jobs"]), 3)
        self.assertEqual([j.status for j in jobs], ["RUNNING", "QUEUED", "QUEUED"])
        prey.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((prey.item_amount, self.bag_a.money), (1, 94))
        for job in jobs:
            self.assertEqual((job.input_slot.item_amount, job.input_slot.quality, job.escrow_bag.money), (1, 4, 2))
        # A player may append more work while previous outputs remain unclaimed.
        later = self.start(prey)
        self.assertEqual(later["status"], "QUEUED")
        self.call("cancel_game_processing", dict(job_id=str(jobs[1].pk)))
        self.advance(105)
        jobs[0].refresh_from_db(); jobs[1].refresh_from_db(); jobs[2].refresh_from_db()
        self.assertEqual([j.status for j in jobs], ["READY", "CANCELLED", "RUNNING"])
        self.advance(110)
        jobs[2].refresh_from_db(); self.tool.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((jobs[2].status, self.tool.remaining_uses, self.bag_a.money), ("READY", 18, 94))
        self.assertEqual(VillagePlayerProcessingJob.objects.get(pk=later["job_id"]).status, "RUNNING")
        self.call("claim_game_processing", dict(job_id=str(jobs[2].pk)))
        jobs[0].refresh_from_db()
        self.assertEqual(jobs[0].status, "READY")

    def test_batch_quote_is_bound_to_quantity_and_validated_without_partial_mutation(self):
        prey = self.slot(item_id=PREY_CHICKEN, quantity=4, quality=3)
        params = dict(prey_instance_id=str(prey.instance_id), quantity=2)
        quote = self.call("preview_game_processing", params)["preview"]
        with self.assertRaises(ProcessingError) as caught:
            self.call("start_game_processing", dict(params, quantity=3, expected_quote=quote["quote_hash"]))
        self.assertEqual(caught.exception.code, "QUOTE_CHANGED")
        for invalid in (0, -1, 101, True, 1.5, "2"):
            with self.assertRaises(ProcessingError):
                self.call("preview_game_processing", dict(params, quantity=invalid))
        self.assertEqual(self.call("preview_game_processing", dict(params, quantity=5))["preview"]["reason"], "PREY_QUANTITY_UNAVAILABLE")
        self.bag_a.money = 3; self.bag_a.save()
        poor = self.call("preview_game_processing", params)["preview"]
        self.assertEqual((poor["available"], poor["max_quantity"], poor["reason"]), (False, 1, "INSUFFICIENT_MONEY"))
        self.assertFalse(VillagePlayerProcessingJob.objects.exists())
        prey.refresh_from_db(); self.assertEqual(prey.item_amount, 4)

    def test_batch_creation_failure_rolls_back_every_escrow_and_fee(self):
        prey = self.slot(item_id=PREY_CHICKEN, quantity=3, quality=3)
        params = dict(prey_instance_id=str(prey.instance_id), quantity=3)
        quote = self.call("preview_game_processing", params)["preview"]
        original = VillagePlayerProcessingJob.objects.create
        count = 0
        def fail_second(**kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise ValueError("INJECTED_BATCH_FAILURE")
            return original(**kwargs)
        before = (InventoryBag.objects.count(), InventorySlot.objects.count(), EconomicLedgerEntry.objects.count())
        with patch.object(VillagePlayerProcessingJob.objects, "create", side_effect=fail_second), self.assertRaises(ValueError):
            self.call("start_game_processing", dict(params, expected_quote=quote["quote_hash"]))
        prey.refresh_from_db(); self.bag_a.refresh_from_db()
        self.assertEqual((prey.item_amount, self.bag_a.money), (3, 100))
        self.assertEqual(before, (InventoryBag.objects.count(), InventorySlot.objects.count(), EconomicLedgerEntry.objects.count()))
        self.assertFalse(VillagePlayerProcessingJob.objects.exists())

    def test_batch_action_replay_returns_original_jobs_without_double_charging(self):
        prey = self.slot(item_id=PREY_CHICKEN, quantity=3, quality=3)
        params = dict(prey_instance_id=str(prey.instance_id), quantity=3)
        quote = self.run_action("preview_game_processing", params)["data"]["result"]["preview"]
        payload = self.payload("start_game_processing", dict(params, expected_quote=quote["quote_hash"]), location="BUTCHER_YARD")
        from village.player_actions import execute_player_action
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        second = execute_player_action(payload)
        self.assertEqual(first, second)
        self.bag_a.refresh_from_db(); prey.refresh_from_db()
        self.assertEqual((VillagePlayerProcessingJob.objects.count(), prey.item_amount, self.bag_a.money), (3, 0, 94))

    def test_waiting_quote_uses_the_reserved_refined_tool_speed(self):
        self.tool.item_id, self.tool.equipment_level, self.tool.remaining_uses = 4131, 5, 80
        self.tool.save(update_fields=["item_id", "equipment_level", "remaining_uses"])
        self.start()
        next_job = self.start()
        self.assertEqual((next_job["duration_minutes"], next_job["status"]), (3, "QUEUED"))

    def test_pending_outputs_include_catalog_names_and_inherited_quality_without_mutation(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from items.models import Item
        from village.player_processing import _entry

        self.rt.current_location_code = "JACK_HOME"
        self.rt.save()
        record = self.start()
        job = VillagePlayerProcessingJob.objects.get(pk=record["job_id"])
        with CaptureQueriesContext(connection) as queries:
            projected = _entry(job, 100)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries))
        names = dict(Item.objects.filter(pk__in=[row["item_id"] for row in projected["outputs"]]).values_list("pk", "item_name"))
        self.assertTrue(projected["outputs"])
        for row in projected["outputs"]:
            self.assertEqual(row["item_name"], names[row["item_id"]])
            self.assertEqual(row["quality"], job.input_snapshot["quality"])
        job.refresh_from_db()
        self.assertEqual(job.output_snapshot, [])

    def test_legacy_running_snapshot_without_equipment_level_still_completes(self):
        record = self.start()
        job = VillagePlayerProcessingJob.objects.get(pk=record["job_id"])
        job.input_snapshot["attributes"].pop("equipment_level")
        job.save(update_fields=["input_snapshot"])
        self.advance(105)
        job.refresh_from_db()
        self.assertEqual(job.status, "READY")

    def test_refined_tool_shortens_work_and_running_duration_survives_config_change(self):
        from player.models import EquipmentLevel
        self.tool.item_id, self.tool.equipment_level, self.tool.remaining_uses = 4131, 5, 80
        self.tool.save(update_fields=["item_id", "equipment_level", "remaining_uses"])
        record = self.start()
        job = VillagePlayerProcessingJob.objects.get(pk=record["job_id"])
        self.assertEqual(job.duration_minutes, 3)
        EquipmentLevel.objects.filter(equipment__item_id=4131, level=5).update(processing_time_reduction=0)
        self.advance(103)
        job.refresh_from_db()
        self.tool.refresh_from_db()
        self.assertEqual((job.status, job.duration_minutes, self.tool.remaining_uses), ("READY", 3, 79))

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
        self.assertTrue(preview["available"])
        self.assertEqual(preview["waiting_reason"], "NPC_BUSY")
        self.call("cancel_game_processing", dict(job_id=job["job_id"]))
        self.rt.active_command_id = uuid.uuid4(); self.rt.save()
        preview = self.call("preview_game_processing", dict(prey_instance_id=str(prey.instance_id)), player=self.b, bag=self.bag_b)["preview"]
        self.assertTrue(preview["available"])
        self.assertEqual(preview["waiting_reason"], "NPC_BUSY")

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
        self.assertEqual((meat.quality, meat.produced_world_minute, meat.expires_world_minute), (4, 90, None))
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

    def test_absent_jack_accepts_escrow_and_only_arrival_starts_work_clock(self):
        self.rt.current_location_code = "JACK_HOME"; self.rt.save()
        row = self.start()
        job = VillagePlayerProcessingJob.objects.get(pk=row["job_id"])
        self.assertEqual((row["status"], row["ready_world_minute"], job.tool_instance_id), ("QUEUED", None, None))
        self.assertFalse(jack_processing_busy("10012"))
        self.assertEqual(get_available_slot_amount(self.tool, 100), 1)
        self.assertEqual(self.advance(106), [])
        self.rt.current_location_code = "BUTCHER_YARD"; self.rt.save()
        self.assertEqual(self.advance(110), [self.a.pk])
        job.refresh_from_db(); self.tool.refresh_from_db()
        self.assertEqual((job.status, job.started_world_minute, job.ready_world_minute, self.tool.remaining_uses), ("RUNNING", 110, 115, 20))
        self.advance(114); job.refresh_from_db(); self.assertEqual(job.status, "RUNNING")
        self.advance(115); job.refresh_from_db(); self.tool.refresh_from_db()
        self.assertEqual((job.status, self.tool.remaining_uses), ("READY", 19))

    def test_waiting_expiry_preserves_assets_for_full_refund(self):
        self.rt.current_location_code = "JACK_HOME"; self.rt.save()
        prey = self.slot(item_id=PREY_CHICKEN, quantity=1, expires_world_minute=108)
        row = self.start(prey)
        self.advance(104)
        job = VillagePlayerProcessingJob.objects.get(pk=row["job_id"])
        self.assertEqual((job.status, job.failure_reason), ("QUEUED", ""))
        self.call("cancel_game_processing", dict(job_id=row["job_id"]))
        self.bag_a.refresh_from_db(); self.tool.refresh_from_db(); job.input_slot.refresh_from_db()
        self.assertEqual((self.bag_a.money, self.tool.remaining_uses, job.input_slot.expires_world_minute), (100, 20, 108))

    def test_multiple_players_queue_without_claiming_the_same_work_slot(self):
        first = self.start()
        second = self.start(self.slot(item_id=PREY_CHICKEN, quantity=1, bag=self.bag_b), player=self.b, bag=self.bag_b)
        self.assertEqual((first["status"], second["status"]), ("RUNNING", "QUEUED"))
        self.assertEqual(VillagePlayerProcessingJob.objects.filter(status="RUNNING").count(), 1)
        self.advance(105)
        queued = VillagePlayerProcessingJob.objects.get(pk=second["job_id"])
        self.assertEqual((queued.status, queued.started_world_minute, queued.ready_world_minute), ("RUNNING", 105, 110))

    def test_boundary_freshness_quote_and_owner_gate(self):
        prey = self.slot(item_id=PREY_CHICKEN, quantity=1, expires_world_minute=105)
        preview = self.call("preview_game_processing", dict(prey_instance_id=str(prey.instance_id)))["preview"]
        self.assertTrue(preview["available"])
        prey.expires_world_minute = 106; prey.save()
        quote = self.call("preview_game_processing", dict(prey_instance_id=str(prey.instance_id)))["preview"]
        prey.quality = 4; prey.save()
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
