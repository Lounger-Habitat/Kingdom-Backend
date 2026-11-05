"""In-memory automatic workshop, physical escrow and independent NPC actions."""
import uuid
import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot, InventoryBag, InventorySlot
from inventory.views import get_or_create_bag
from rest_framework.exceptions import ValidationError
from player.models import PlayerGrowthState, SkillRewardReceipt
from recipes.models import Recipe
from village.models import (NpcRuntimeState, VillageActionDurationConfig,
                            VillagePlayerProcessingJob, EconomicLedgerEntry, NpcProfile, VillageSimulationConfig)
from village.phase3_catalog import PREY_CHICKEN, PREY_DEER, VENISON
from village.phase3_asset_service import get_available_slot_amount
from village.player_processing import (execute_processing_action, processing_overview,
    settle_processing_jobs, ProcessingError)
from village.tests.test_player_actions import PlayerFixture


class PlayerProcessingTests(PlayerFixture, TestCase):
    def test_claim_all_only_receives_own_ready_jobs_and_replays_without_duplicate_rewards(self):
        from village.player_actions import execute_player_action

        ready = [self.start() for _ in range(3)]
        foreign = self.start(self.slot(item_id=PREY_CHICKEN, quantity=1, bag=self.bag_b), player=self.b, bag=self.bag_b)
        self.advance(120)
        single = self.call("claim_game_processing", dict(job_id=ready[0]["job_id"]))
        self.assertEqual(single["claimed_count"], 1)
        running, queued, cancelled, failed = [self.start() for _ in range(4)]
        self.call("cancel_game_processing", dict(job_id=cancelled["job_id"]))
        VillagePlayerProcessingJob.objects.filter(pk=failed["job_id"]).update(status="FAILED", failure_reason="PROCESSING_ESCROW_INVALID")
        payload = self.payload("claim_game_processing", dict(all_ready=True), location="BUTCHER_YARD")
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        result = first["data"]["result"]
        self.assertEqual(result["claimed_count"], 2)
        self.assertEqual({row["job_id"] for row in result["jobs"]}, {row["job_id"] for row in ready[1:]})
        self.assertEqual(sum(row["quantity"] for row in result["claimed_items"] if row["item_id"] == 1056), 6)
        self.assertEqual(first["data"]["settlement_section"], "processing")
        for row, status in [(foreign, "READY"), (running, "RUNNING"), (queued, "QUEUED"), (cancelled, "CANCELLED"), (failed, "FAILED")]:
            self.assertEqual(VillagePlayerProcessingJob.objects.get(pk=row["job_id"]).status, status)
        self.assertEqual(execute_player_action(payload), dict(first, replayed=True))
        self.assertEqual(self.bag_a.slots.get(item_id=1056, item_amount__gt=0).item_amount, 9)
        self.assertEqual(self.bag_a.slots.get(item_id=29104, item_amount__gt=0).item_amount, 3)

    def test_claim_all_rolls_back_every_transfer_if_any_escrow_is_invalid(self):
        rows = [self.start() for _ in range(2)]
        self.advance(110)
        broken = VillagePlayerProcessingJob.objects.get(pk=rows[1]["job_id"])
        broken.escrow_bag.slots.filter(instance_id=broken.output_snapshot[0]["instance_id"]).update(item_amount=0)
        before = list(self.bag_a.slots.order_by("pk").values_list("pk", "item_amount"))
        ledger_count = EconomicLedgerEntry.objects.count()
        with self.assertRaises(ProcessingError) as caught:
            self.call("claim_game_processing", dict(all_ready=True))
        self.assertEqual(caught.exception.code, "PROCESSING_ESCROW_INVALID")
        self.assertEqual(list(self.bag_a.slots.order_by("pk").values_list("pk", "item_amount")), before)
        self.assertEqual(EconomicLedgerEntry.objects.count(), ledger_count)
        self.assertEqual(list(VillagePlayerProcessingJob.objects.values_list("status", flat=True)), ["READY", "READY"])

    def test_claim_all_requires_explicit_scope_and_an_available_job(self):
        for params in ({}, {"all_ready": False}, {"all_ready": 1}, {"all_ready": "true"},
                       {"all_ready": True, "job_id": str(uuid.uuid4())}, {"all_ready": True, "quantity": 1}):
            with self.subTest(params=params), self.assertRaises(ProcessingError) as caught:
                self.call("claim_game_processing", params)
            self.assertEqual(caught.exception.code, "INVALID_PARAMS")
        with self.assertRaises(ProcessingError) as caught:
            self.call("claim_game_processing", dict(all_ready=True))
        self.assertEqual(caught.exception.code, "PROCESSING_NOT_READY")
        rejected = self.run_action("claim_game_processing", dict(all_ready=True), location="RESTAURANT")
        self.assertFalse(rejected["success"])

    def test_processing_names_use_catalog_instead_of_inventory_storage_names(self):
        from items.models import Item

        name = Item.objects.get(pk=PREY_CHICKEN).item_name
        prey = self.slot(item_id=PREY_CHICKEN, quantity=1, quality=4,
            item_bag_name=f"{name}_{uuid.uuid4().hex}")
        overview = processing_overview(self.a, self.bag_a, 100)
        self.assertEqual(overview["inputs"][0]["item_name"], name)
        record = self.start(prey)
        self.assertEqual(record["input"]["item_name"], name)
        self.advance(105)
        job = VillagePlayerProcessingJob.objects.get(pk=record["job_id"])
        self.assertEqual(job.status, "READY")
        names = dict(Item.objects.filter(pk__in=[row["item_id"] for row in job.output_snapshot])
            .values_list("pk", "item_name"))
        for row in job.output_snapshot:
            self.assertEqual(row["item_name"], names[row["item_id"]])

    def test_legacy_job_names_are_repaired_without_mutating_frozen_snapshots(self):
        import copy
        from items.models import Item

        record = self.start()
        self.advance(105)
        job = VillagePlayerProcessingJob.objects.get(pk=record["job_id"])
        for row in [job.input_snapshot, *job.output_snapshot]:
            row["item_name"] = f"{row['item_name']}_{uuid.uuid4().hex}_{uuid.uuid4().hex}"
        job.save(update_fields=["input_snapshot", "output_snapshot"])
        frozen = copy.deepcopy((job.input_snapshot, job.output_snapshot))
        overview = processing_overview(self.a, self.bag_a, 105)
        projected = next(row for row in overview["jobs"] if row["job_id"] == record["job_id"])
        for row in [projected["input"], *projected["outputs"]]:
            self.assertEqual(row["item_name"], Item.objects.get(pk=row["item_id"]).item_name)
        job.refresh_from_db()
        self.assertEqual((job.input_snapshot, job.output_snapshot), frozen)
        self.call("claim_game_processing", dict(job_id=record["job_id"]))
        job.refresh_from_db()
        self.assertEqual(job.status, "CLAIMED")

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
        self.assertEqual((jobs[2].status, self.tool.remaining_uses, self.bag_a.money), ("READY", 20, 94))
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
        self.assertEqual(dict(first, replayed=True), second)
        self.assertEqual(first["data"]["settlement_section"], "processing")
        self.assertTrue(first["data"]["overview_partial"])
        self.assertNotIn("inventory_partial", first["data"])
        self.assertNotIn(str(prey.instance_id), [row["instance_id"] for row in first["data"]["overview"]["inventory"]])
        self.bag_a.refresh_from_db(); prey.refresh_from_db()
        self.assertEqual((VillagePlayerProcessingJob.objects.count(), prey.item_amount, self.bag_a.money), (3, 0, 94))

    def test_workshop_quotes_do_not_depend_on_jack_tool_upgrades(self):
        prey = self.slot(item_id=PREY_CHICKEN, quantity=2, quality=3)
        params = dict(prey_instance_id=str(prey.instance_id))
        before = self.call("preview_game_processing", params)["preview"]
        self.tool.item_id, self.tool.equipment_level, self.tool.remaining_uses = 4131, 5, 80
        self.tool.save(update_fields=["item_id", "equipment_level", "remaining_uses"])
        after = self.call("preview_game_processing", params)["preview"]
        self.assertEqual(before["quote_hash"], after["quote_hash"])
        self.start()
        next_job = self.start()
        self.assertEqual((next_job["duration_minutes"], next_job["status"]), (5, "QUEUED"))

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

    def test_accepted_workshop_duration_survives_config_change(self):
        record = self.start()
        job = VillagePlayerProcessingJob.objects.get(pk=record["job_id"])
        VillageActionDurationConfig.objects.filter(action_name="PROCESS_GAME").update(duration_minutes=20)
        self.assertEqual(self.start()["duration_minutes"], 20)
        self.advance(105)
        job.refresh_from_db()
        self.tool.refresh_from_db()
        self.assertEqual((job.status, job.duration_minutes, self.tool.remaining_uses), ("READY", 5, 20))

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
        self.assertEqual(job.input_slot.item_amount, 0)
        returned = self.bag_a.slots.get(item_id=PREY_CHICKEN, item_amount=2)
        self.assertEqual(returned.quality, prey.quality)
        self.assertEqual((returned.produced_world_minute, returned.expires_world_minute, returned.source_request_id), (None, None, None))

    def test_workshop_queue_does_not_reserve_jacks_personal_tool(self):
        job = self.start()
        self.assertIsNone(VillagePlayerProcessingJob.objects.get(pk=job["job_id"]).tool_instance_id)
        self.assertEqual(get_available_slot_amount(self.tool, 100), 1)
        prey = self.slot(item_id=PREY_CHICKEN, quantity=1, bag=self.bag_b)
        preview = self.call("preview_game_processing", dict(prey_instance_id=str(prey.instance_id)), player=self.b, bag=self.bag_b)["preview"]
        self.assertTrue(preview["available"])
        self.assertEqual(preview["waiting_reason"], "PROCESSING_QUEUE_BUSY")
        self.call("cancel_game_processing", dict(job_id=job["job_id"]))
        self.rt.active_command_id = uuid.uuid4(); self.rt.save()
        preview = self.call("preview_game_processing", dict(prey_instance_id=str(prey.instance_id)), player=self.b, bag=self.bag_b)["preview"]
        self.assertTrue(preview["available"])
        self.assertEqual(preview["waiting_reason"], "")

    def test_completion_pays_once_without_tool_wear_and_late_claim_keeps_quality(self):
        prey = self.slot(item_id=PREY_DEER, quantity=1, quality=4, expires_world_minute=120)
        row = self.start(prey)
        self.assertEqual(self.advance(105), [self.a.pk])
        self.assertEqual(self.advance(105), [])
        self.jack.refresh_from_db(); self.tool.refresh_from_db()
        self.assertEqual((self.jack.money, self.tool.remaining_uses), (245, 20))
        job = VillagePlayerProcessingJob.objects.get(pk=row["job_id"])
        self.assertEqual(job.status, "READY")
        rev = PlayerGrowthState.objects.get(player=self.a).revision
        self.advance(130)
        self.assertEqual(PlayerGrowthState.objects.get(player=self.a).revision, rev)
        self.call("claim_game_processing", dict(job_id=row["job_id"]))
        meat = self.bag_a.slots.get(item_id=VENISON, item_amount=4)
        self.assertEqual((meat.quality, meat.produced_world_minute, meat.expires_world_minute), (4, None, None))
        self.assertFalse(meat.dish_metadata)
        self.assertEqual(self.bag_a.slots.get(item_id=29105, item_amount=1).expires_world_minute, None)
        self.assertFalse(SkillRewardReceipt.objects.filter(player=self.a).exists())
        with self.assertRaises(ProcessingError):
            self.call("claim_game_processing", dict(job_id=row["job_id"]))
        self.jack.refresh_from_db(); self.tool.refresh_from_db()
        self.assertEqual((self.jack.money, self.tool.remaining_uses), (245, 20))

    def test_broken_personal_tool_does_not_interrupt_workshop(self):
        row = self.start()
        self.tool.remaining_uses = 0; self.tool.save()
        self.assertEqual(self.advance(105), [self.a.pk])
        job = VillagePlayerProcessingJob.objects.get(pk=row["job_id"])
        self.assertEqual((job.status, job.escrow_bag.money, job.input_slot.item_amount), ("READY", 0, 0))
        self.jack.refresh_from_db(); self.assertEqual(self.jack.money, 242)
        self.call("claim_game_processing", dict(job_id=row["job_id"]))
        self.assertEqual(self.bag_a.slots.get(item_id=1056, item_amount=3).quality, 3)

    def test_absent_sleeping_busy_or_unhealthy_jack_does_not_delay_or_power_workshop(self):
        self.tool.delete()
        for status in ("AWAY", "SLEEPING", "RUNNING"):
            with self.subTest(status=status):
                self.rt.current_location_code = "JACK_HOME"
                self.rt.action_status = status
                self.rt.active_command_id = uuid.uuid4() if status == "RUNNING" else None
                self.rt.away_until_world_minute = self.clock.world_minute + 100
                self.rt.health_fp, self.rt.energy_fp, self.rt.hunger_fp = 0, 0, 1000
                self.rt.save()
                before = NpcRuntimeState.objects.filter(pk=self.rt.pk).values().get()
                row = self.start()
                self.assertEqual((row["status"], row["started_world_minute"], row["ready_world_minute"]),
                                 ("RUNNING", self.clock.world_minute, self.clock.world_minute + 5))
                self.advance(row["ready_world_minute"])
                self.assertEqual(VillagePlayerProcessingJob.objects.get(pk=row["job_id"]).status, "READY")
                self.assertEqual(NpcRuntimeState.objects.filter(pk=self.rt.pk).values().get(), before)

    def test_queued_cancellation_preserves_quality_and_refunds_full_fee(self):
        self.start(self.slot(item_id=PREY_CHICKEN, quantity=1, bag=self.bag_b), player=self.b, bag=self.bag_b)
        prey = self.slot(item_id=PREY_CHICKEN, quantity=1, expires_world_minute=108)
        row = self.start(prey)
        self.advance(104)
        job = VillagePlayerProcessingJob.objects.get(pk=row["job_id"])
        self.assertEqual((job.status, job.failure_reason), ("QUEUED", ""))
        self.call("cancel_game_processing", dict(job_id=row["job_id"]))
        self.bag_a.refresh_from_db(); self.tool.refresh_from_db(); job.input_slot.refresh_from_db()
        returned = self.bag_a.slots.get(item_id=PREY_CHICKEN, item_amount=1)
        self.assertEqual((self.bag_a.money, self.tool.remaining_uses, returned.quality, returned.expires_world_minute), (100, 20, prey.quality, None))

    def test_multiple_players_queue_without_claiming_the_same_work_slot(self):
        first = self.start()
        second = self.start(self.slot(item_id=PREY_CHICKEN, quantity=1, bag=self.bag_b), player=self.b, bag=self.bag_b)
        self.assertEqual((first["status"], second["status"]), ("RUNNING", "QUEUED"))
        self.assertEqual(VillagePlayerProcessingJob.objects.filter(status="RUNNING").count(), 1)
        self.advance(105)
        queued = VillagePlayerProcessingJob.objects.get(pk=second["job_id"])
        self.assertEqual((queued.status, queued.started_world_minute, queued.ready_world_minute), ("RUNNING", 105, 110))

    def test_delayed_tick_completes_all_due_jobs_on_the_original_queue_timeline(self):
        rows = [self.start() for _ in range(3)]
        self.advance(115)
        jobs = [VillagePlayerProcessingJob.objects.get(pk=row["job_id"]) for row in rows]
        self.assertEqual([job.status for job in jobs], ["READY"] * 3)
        self.assertEqual([job.started_world_minute for job in jobs], [100, 105, 110])
        self.assertEqual([job.completed_world_minute for job in jobs], [105, 110, 115])
        self.jack.refresh_from_db()
        self.assertEqual(self.jack.money, 246)
        self.assertEqual(self.advance(115), [])

    def test_cancelling_running_job_immediately_starts_the_next_players_job(self):
        first = self.start()
        second = self.start(self.slot(item_id=PREY_CHICKEN, quantity=1, bag=self.bag_b), player=self.b, bag=self.bag_b)
        self.clock.world_minute = 101
        self.clock.save()
        self.call("cancel_game_processing", dict(job_id=first["job_id"]))
        queued = VillagePlayerProcessingJob.objects.get(pk=second["job_id"])
        self.assertEqual((queued.status, queued.started_world_minute, queued.ready_world_minute), ("RUNNING", 101, 106))
        self.advance(106)
        queued.refresh_from_db(); self.bag_a.refresh_from_db(); self.jack.refresh_from_db()
        self.assertEqual((queued.status, self.bag_a.money, self.jack.money), ("READY", 100, 242))

    def test_legacy_queued_jobs_resume_without_npc_runtime_or_tools(self):
        # Reproduce an old waiting job, which has never entered begin_work.
        with patch("village.player_processing._start_queued_jobs", return_value=[]):
            row = self.start()
        job = VillagePlayerProcessingJob.objects.get(pk=row["job_id"])
        self.assertEqual(job.status, "QUEUED")
        self.rt.delete()
        self.tool.delete()
        self.advance(110)
        job.refresh_from_db()
        self.assertEqual((job.status, job.started_world_minute, job.ready_world_minute, job.tool_instance_id),
                         ("RUNNING", 110, 115, None))
        self.advance(115)
        job.refresh_from_db()
        self.assertEqual(job.status, "READY")

    @override_settings(VILLAGE_FAST_COMMAND_INTEGRATION=False)
    def test_jack_can_process_his_own_prey_while_the_workshop_is_processing(self):
        from village.star_actions import accept_action
        from village.runtime_services import CommandService
        self.start()
        VillageSimulationConfig.objects.filter(config_key="default").update(village_needs_enabled=True)
        NpcProfile.objects.update_or_create(npc=self.rt.npc, defaults=dict(display_name="Jack", is_active=True,
            home_location_code="JACK_HOME", work_location_code="BUTCHER_YARD", social_location_code="VILLAGE_SQUARE",
            capabilities=["PROCESS_GAME"]))
        own_prey = NpcGlobalInventorySlot.objects.create(bag=self.jack, slot_index=1,
            item_id=PREY_CHICKEN, item_amount=1, quality=3)
        envelope = dict(id=str(uuid.uuid4()), timestamp=int(time.time() * 1000), type="message", sender="10012", recipient="env",
            payload=dict(type="action", content=dict(id="jack-process-with-workshop", name="PROCESS_GAME",
                params=dict(prey_instance_id=str(own_prey.instance_id)))))
        ok, code, task, _ = accept_action(envelope)
        self.assertTrue(ok, code)
        ok, code, _ = CommandService.start_command(task.command_id)
        self.assertTrue(ok, code)
        self.assertEqual(get_available_slot_amount(self.tool, 100), 0)
        self.advance(105)
        self.assertEqual(VillagePlayerProcessingJob.objects.get().status, "READY")

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
