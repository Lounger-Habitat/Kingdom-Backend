"""Real sample conservation, Mark authority, and private persistent results."""
import copy
import json
import time
import uuid
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError

from inventory.components import BATCH_COPY_FIELDS
from inventory.models import InventoryBag, InventorySlot, NpcDefinition
from inventory.views import get_or_create_bag
from player.models import PlayerGrowthState, PlayerMilestoneProgress, PlayerSkillProgress, SkillRewardReceipt
from village.lucy_cooking import pending_reviews, rate_dish
from village.models import (NpcProfile, NpcRuntimeState, VillageActionDurationConfig, VillageDeliveryOutbox,
    VillagePlayerActionReceipt, VillagePlayerDishReview, VillageWorldEvent)
from village.player_cooking import RULE_VERSION
from village.player_dish_reviews import (ReviewError, complete_player_review, execute_review_action,
    review_overview)
from village.runtime_services import CommandService
from village.phase3_asset_service import commit_economy_action
from village.tests.test_phase3_production import _payload
from village.tests.test_player_actions import PlayerFixture


class PlayerDishReviewTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.mark, _ = NpcDefinition.objects.get_or_create(agent_id="10013", defaults=dict(name="Mark"))
        NpcRuntimeState.objects.update_or_create(npc=self.mark, defaults=dict(
            current_location_code="RECORD_HALL", health_fp=1000, action_status="IDLE",
            energy_fp=800, needs_world_minute=100, away_until_world_minute=None))
        NpcProfile.objects.update_or_create(npc=self.mark, defaults=dict(display_name="Mark", occupation="点评师",
            home_location_code="RECORD_HALL", work_location_code="RECORD_HALL",
            social_location_code="RECORD_HALL", capabilities=["RATE_DISH", "INSPECT_RATEABLE_DISHES"]))
        self.scores = dict(color=8, aroma=7, taste=9, presentation=8)

    def dish(self, amount=2, *, own=False, bag=None, **extra):
        rid = uuid.uuid4()
        metadata = dict(rule_version=RULE_VERSION, maker_player_id=str(self.a.pk),
            production_request_id=str(rid), production_batch_id=str(uuid.uuid4()), recipe_id=29201,
            recipe=dict(version=1, difficulty=1), ingredients_exact=True, standard_batches=1,
            ingredients=[dict(item_id=1030, item_name="玉米", quantity=1, instance_id=str(uuid.uuid4()))])
        slot = self.slot(quantity=amount, item_id=29201, bag=bag, dish_quality=4,
            ingredient="玉米×1", cook_time=21, dish_metadata=metadata, source_request_id=rid, **extra)
        if own:
            VillagePlayerActionReceipt.objects.create(player=self.a, request_id=rid, action="cook_dish",
                payload_hash="x" * 64, world_minute=90, response=dict(success=True, data=dict(result=dict(
                    dish=dict(item_id=29201, quantity=amount, quality=slot.quality,
                        produced_world_minute=slot.produced_world_minute, expires_world_minute=slot.expires_world_minute,
                        freshness_unknown=slot.freshness_unknown, dish_metadata=copy.deepcopy(metadata))))))
        return slot

    def act(self, action, params, *, player=None, bag=None, rid=None):
        return execute_review_action(player or self.a, bag or self.bag_a, action, params, rid or uuid.uuid4(), self.clock)

    def request(self, slot, **kwargs):
        data = self.act("request_dish_review", dict(instance_id=str(slot.instance_id)), **kwargs)
        return VillagePlayerDishReview.objects.get(pk=data["review"]["review_id"])

    def reply(self, row, actor="10013", **kwargs):
        return rate_dish(actor, dict(review_id=str(row.pk), scores=self.scores, comment="真实玉米清甜，火候适宜。", **kwargs),
                         self.clock.world_minute, uuid.uuid4())

    def test_real_one_portion_escrow_preserves_all_batch_fields_and_notifies_only_mark(self):
        slot = self.dish()
        attrs = {name: copy.deepcopy(getattr(slot, name)) for name in BATCH_COPY_FIELDS}
        rid = uuid.uuid4()
        row = self.request(slot, rid=rid)
        slot.refresh_from_db()
        self.assertEqual(slot.item_amount, 1)
        self.assertNotEqual(row.sample_slot.bag_id, self.bag_a.pk)
        self.assertEqual(row.sample_slot.bag.player_id, self.a.pk)
        self.assertEqual(row.sample_slot.item_amount, 1)
        self.assertEqual({name: getattr(row.sample_slot, name) for name in BATCH_COPY_FIELDS}, attrs)
        self.assertEqual(self.request(slot, rid=rid).pk, row.pk)
        self.assertEqual(VillagePlayerDishReview.objects.count(), 1)
        self.assertEqual(list(VillageDeliveryOutbox.objects.filter(event__event_type="PLAYER_DISH_REVIEW_REQUESTED")
            .values_list("recipient", flat=True)), ["10013"])
        payload = json.dumps(VillageDeliveryOutbox.objects.get(event__event_type="PLAYER_DISH_REVIEW_REQUESTED").payload)
        self.assertNotIn(str(slot.instance_id), payload)
        self.assertNotIn(str(slot.source_request_id), payload)
        with self.assertRaises(ValidationError):
            get_or_create_bag(self.a, row.sample_slot.bag.character_id)

    def test_cancel_and_claim_preserve_original_batch_and_never_duplicate(self):
        slot = self.dish()
        attrs = {name: copy.deepcopy(getattr(slot, name)) for name in BATCH_COPY_FIELDS}
        row = self.request(slot)
        self.act("cancel_dish_review", dict(review_id=str(row.pk)))
        result = self.act("claim_dish_review", dict(review_id=str(row.pk)))
        returned = self.bag_a.slots.get(instance_id=result["returned_instance_id"])
        self.assertEqual({name: getattr(returned, name) for name in BATCH_COPY_FIELDS}, attrs)
        self.assertEqual(self.bag_a.slots.filter(item_id=29201).count(), 2)
        self.assertEqual(sum(self.bag_a.slots.filter(item_id=29201).values_list("item_amount", flat=True)), 2)
        with self.assertRaises(ReviewError) as error:
            self.act("claim_dish_review", dict(review_id=str(row.pk)))
        self.assertEqual(error.exception.code, "DISH_REVIEW_ALREADY_CLAIMED")
        row.refresh_from_db()
        self.assertEqual(row.status, "CANCELLED")
        self.assertEqual(row.sample_slot.item_amount, 0)
        self.assertFalse(review_overview(self.a, self.bag_a, 100)["entries"][0]["can_claim"])

    def test_expiry_projects_without_writes_then_claims_still_expired_and_releases_pending_limit(self):
        row = self.request(self.dish(expires_world_minute=120))
        self.clock.world_minute = 120
        before = PlayerGrowthState.objects.get(player=self.a).revision
        with CaptureQueriesContext(connection) as queries:
            projected = review_overview(self.a, self.bag_a, 120, read_only=True)
            mark = pending_reviews(self.mark, 120)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries))
        self.assertEqual(projected["entries"][0]["status"], "EXPIRED")
        self.assertTrue(projected["entries"][0]["can_claim"])
        self.assertEqual(mark["reviews"], [])
        row.refresh_from_db()
        self.assertEqual(row.status, "PENDING")
        self.assertEqual(PlayerGrowthState.objects.get(player=self.a).revision, before)
        next_row = self.request(self.dish())
        self.assertNotEqual(next_row.pk, row.pk)
        result = self.act("claim_dish_review", dict(review_id=str(row.pk)))
        returned = self.bag_a.slots.get(instance_id=result["returned_instance_id"])
        self.assertEqual(returned.expires_world_minute, 120)
        self.assertEqual(returned.produced_world_minute, 90)
        self.assertFalse(returned.rated)

    def test_ownership_one_pending_and_unavailable_mark_have_no_partial_writes(self):
        other = self.dish(bag=self.bag_b)
        with self.assertRaises(ReviewError):
            self.request(other)
        slot = self.dish()
        row = self.request(slot)
        with self.assertRaises(ReviewError) as error:
            self.request(slot)
        self.assertEqual(error.exception.code, "DISH_REVIEW_PENDING")
        with self.assertRaises(ReviewError) as error:
            self.act("cancel_dish_review", dict(review_id=str(row.pk)), player=self.b, bag=self.bag_b)
        self.assertEqual(error.exception.code, "DISH_REVIEW_NOT_FOUND")
        self.assertEqual(review_overview(self.b, self.bag_b, 100)["entries"], [])
        self.act("cancel_dish_review", dict(review_id=str(row.pk)))
        NpcRuntimeState.objects.filter(npc=self.mark).update(action_status="SLEEPING")
        before = (InventoryBag.objects.count(), VillagePlayerDishReview.objects.count())
        with self.assertRaises(ReviewError) as error:
            self.request(slot)
        self.assertEqual(error.exception.code, "MARK_UNAVAILABLE")
        self.assertEqual(before, (InventoryBag.objects.count(), VillagePlayerDishReview.objects.count()))

    def test_invalid_samples_scores_and_actor_do_not_mutate_or_award(self):
        for attrs, code in [(dict(freshness_unknown=True), "DISH_FRESHNESS_UNKNOWN"),
                            (dict(expires_world_minute=100), "DISH_REVIEW_SAMPLE_EXPIRED"),
                            (dict(rated=True), "DISH_ALREADY_REVIEWED")]:
            with self.subTest(code=code), self.assertRaises(ReviewError) as error:
                self.request(self.dish(**attrs))
            self.assertEqual(error.exception.code, code)
        row = self.request(self.dish(own=True))
        self.assertEqual(self.reply(row, actor="10014")["result_code"], "P3_FORBIDDEN_ACTOR")
        for invalid in (True, float("nan"), 0, 11):
            with self.assertRaises(ReviewError):
                complete_player_review("10013", dict(review_id=str(row.pk), scores=dict(self.scores, color=invalid), comment="测试"), 100, uuid.uuid4())
        row.refresh_from_db()
        self.assertEqual(row.status, "PENDING")
        self.assertFalse(row.sample_slot.rated)
        self.assertFalse(SkillRewardReceipt.objects.filter(source_type="dish_review").exists())

    def test_mark_query_is_private_and_read_only_and_first_real_self_production_gets_only_twenty(self):
        row = self.request(self.dish(own=True))
        with CaptureQueriesContext(connection) as queries:
            visible = pending_reviews(self.mark, 100)
            invisible = pending_reviews(self.npc, 100, review_id=str(row.pk))
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries))
        self.assertEqual(invisible["reviews"], [])
        self.assertEqual(visible["reviews"][0]["owner_type"], "PLAYER")
        self.assertNotIn("instance_id", json.dumps(visible))
        revision = PlayerGrowthState.objects.get(player=self.a).revision
        result = self.reply(row)
        self.assertEqual(result["result_code"], "OK", result)
        row.refresh_from_db()
        self.assertTrue(row.own_production_verified)
        self.assertEqual(row.growth_reward["xp_gained"], 20)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="cooking").total_xp, 20)
        self.assertGreater(PlayerGrowthState.objects.get(player=self.a).revision, revision)
        self.assertGreater(row.rating_price, 0)
        self.assertEqual(row.scores, self.scores)
        self.assertEqual(self.reply(row)["result_code"], "DISH_REVIEW_NOT_PENDING")
        claimed = self.act("claim_dish_review", dict(review_id=str(row.pk)))
        returned = self.bag_a.slots.get(instance_id=claimed["returned_instance_id"])
        self.assertTrue(returned.rated)
        self.assertEqual(returned.dish_metadata["review_id"], str(row.pk))
        self.assertEqual(returned.expires_world_minute, 2000)
        second = self.request(self.dish(own=True))
        self.assertEqual(self.reply(second)["result_code"], "OK")
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="cooking").total_xp, 20)
        self.assertEqual(PlayerMilestoneProgress.objects.filter(player=self.a, code="first_formal_dish_review").count(), 1)

    def test_copied_author_metadata_without_receipt_or_other_player_receipt_gets_no_self_exam(self):
        for receipt_player in (None, self.b):
            slot = self.dish()
            if receipt_player:
                VillagePlayerActionReceipt.objects.create(player=receipt_player, request_id=slot.source_request_id,
                    action="cook_dish", payload_hash="x" * 64, world_minute=90,
                    response=dict(success=True, data=dict(result=dict(dish=dict(item_id=slot.item_id,
                        dish_metadata=slot.dish_metadata)))))
            row = self.request(slot)
            revision = PlayerGrowthState.objects.get(player=self.a).revision
            self.assertEqual(self.reply(row)["result_code"], "OK")
            row.refresh_from_db()
            self.assertFalse(row.own_production_verified)
            self.assertEqual(row.growth_reward, {})
            self.assertGreater(PlayerGrowthState.objects.get(player=self.a).revision, revision)
            self.assertFalse(SkillRewardReceipt.objects.filter(source_type="dish_review").exists())

    def test_failed_notification_and_reward_roll_back_real_sample_and_scoring(self):
        slot = self.dish(own=True)
        with patch("village.phase4_visibility.enqueue_event_deliveries", side_effect=RuntimeError("outbox unavailable")):
            with self.assertRaises(RuntimeError):
                self.request(slot)
        slot.refresh_from_db()
        self.assertEqual(slot.item_amount, 2)
        self.assertFalse(VillagePlayerDishReview.objects.exists())
        row = self.request(slot)
        with patch("village.player_dish_reviews.award_skill", side_effect=RuntimeError("growth unavailable")):
            with self.assertRaises(RuntimeError):
                self.reply(row)
        row.refresh_from_db()
        self.assertEqual(row.status, "PENDING")
        self.assertFalse(row.sample_slot.rated)
        self.assertEqual(self.reply(row)["result_code"], "OK")

    def test_real_mark_command_settles_once_and_remains_queryable_after_claim(self):
        row = self.request(self.dish(own=True))
        VillageActionDurationConfig.objects.update_or_create(action_name="RATE_DISH", defaults=dict(duration_minutes=1))
        runtime = NpcRuntimeState.objects.get(npc=self.mark)
        content = dict(schema_version="village.command.v1", id=str(uuid.uuid4()), request_id=str(uuid.uuid4()),
            name="RATE_DISH", params=dict(review_id=str(row.pk), scores=self.scores, comment="真实菜品的正式评价。"),
            queue_mode="STRICT", expected_state_version=runtime.state_version, ttl_ms=86400000)
        ok, code, command = CommandService.accept_command(str(uuid.uuid4()), "10013", "env", content, int(time.time() * 1000))
        self.assertTrue(ok, code)
        ok, code, command = CommandService.start_command(command.command_id)
        self.assertTrue(ok, code)
        self.clock.world_minute = command.started_world_minute + command.duration_minutes
        self.clock.save()
        payload = _payload("RATE_DISH", 10013, "RECORD_HALL", command.sanitized_parameters, command.request_id)
        payload.update(command_id=str(command.command_id), message_id=str(command.message_id))
        result = commit_economy_action(payload)
        self.assertEqual(result["result_code"], "OK", result)
        self.assertTrue(commit_economy_action(payload)["replayed"])
        self.assertEqual(SkillRewardReceipt.objects.filter(source_type="dish_review").count(), 1)
        self.assertEqual(list(VillageWorldEvent.objects.filter(event_type="ECONOMY_RATE_DISH")
            .values_list("visibility", flat=True)), ["PARTICIPANTS"])
        self.act("claim_dish_review", dict(review_id=str(row.pk)))
        saved = pending_reviews(self.mark, self.clock.world_minute, review_id=str(row.pk))["reviews"][0]
        self.assertEqual(saved["status"], "COMPLETED")
        self.assertEqual(saved["scores"], self.scores)
        self.assertGreater(saved["rating_price"], 0)
