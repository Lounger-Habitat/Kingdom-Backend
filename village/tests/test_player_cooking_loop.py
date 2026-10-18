"""One real production chain through the player action/receipt boundary."""
import copy
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from inventory.models import InventorySlot, NpcDefinition
from player.models import PlayerMilestoneProgress, PlayerSkillProgress, SkillRewardReceipt
from recipes.models import Recipe, RecipeIngredient
from village.lucy_cooking import pending_reviews, rate_dish
from village.models import NpcRuntimeState, VillageFarmPlot, VillagePlayerActionReceipt
from village.personal_commissions import publish_commission
from village.player_actions import execute_player_action
from village.tests.test_player_actions import PlayerFixture


class PlayerCookingLoopTests(PlayerFixture, TestCase):
    def test_grown_batch_cooked_reviewed_and_delivered_preserves_assets_provenance_and_rewards(self):
        plot = VillageFarmPlot.objects.create(plot_code="G3_LOOP", lease_enabled=True,
            lessee_player=self.a, leased_world_minute=100, lease_ends_world_minute=10000)
        farm_tool = InventorySlot.objects.create(bag=self.bag_a, slot_index=0, item_id=4001,
                                                item_amount=1, remaining_uses=20)
        cooker = InventorySlot.objects.create(bag=self.bag_a, slot_index=1, item_id=4106,
                                            item_amount=1, remaining_uses=20)
        InventorySlot.objects.create(bag=self.bag_a, slot_index=2, item_id=1051, item_amount=1)
        recipe, _ = Recipe.objects.update_or_create(pk=29201, defaults=dict(item_id=29201,
            recipe_name="清蒸玉米", village_only=True, output_quantity=2, default_dish_quality=3,
            default_cook_time=60))
        recipe.ingredients.all().delete()
        RecipeIngredient.objects.create(recipe=recipe, item_id=1030, item_name="玉米", count=2)
        planted = self.run_action("plant_farm_plot", dict(plot_code=plot.plot_code, crop_code="CORN",
            expected_plot_version=plot.plot_version, tool_instance_id=str(farm_tool.instance_id)), location="BILL_FARM")
        self.assertTrue(planted["success"], (planted.get("result_code"), planted.get("message")))
        plot.refresh_from_db()
        self.clock.world_minute = plot.mature_world_minute
        self.clock.save(update_fields=["world_minute"])
        harvested = self.run_action("harvest_farm_plot", dict(plot_code=plot.plot_code,
            expected_plot_version=plot.plot_version, expected_batch_id=str(plot.batch_id),
            tool_instance_id=str(farm_tool.instance_id)), location="BILL_FARM")
        self.assertTrue(harvested["success"], (harvested.get("result_code"), harvested.get("message")))
        raw = self.bag_a.slots.get(item_id=1030)
        params = dict(recipe_id=29201, batches=1, tool_instance_id=str(cooker.instance_id),
                      selections=[dict(instance_id=str(raw.instance_id), quantity=2)])
        with CaptureQueriesContext(connection) as queries:
            preview = self.run_action("preview_cooking", params)["data"]["result"]["preview"]
        self.assertTrue(preview["available"], preview)
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertFalse(VillagePlayerActionReceipt.objects.filter(player=self.a, action="preview_cooking").exists())
        payload = self.payload("cook_dish", dict(params, expected_quote=preview["quote_hash"]), location="RESTAURANT")
        cooked = execute_player_action(payload)
        self.assertTrue(cooked["success"], (cooked.get("result_code"), cooked.get("message")))
        self.assertEqual(cooked, execute_player_action(copy.deepcopy(payload)))
        dish = cooked["data"]["result"]["dish"]
        self.assertEqual(dish["quantity"], 2)
        self.assertEqual(dish["expires_world_minute"], raw.expires_world_minute)
        cooker.refresh_from_db()
        self.assertEqual(cooker.remaining_uses, 19)
        mark, _ = NpcDefinition.objects.get_or_create(agent_id="10013", defaults=dict(name="Mark"))
        NpcRuntimeState.objects.update_or_create(npc=mark, defaults=dict(current_location_code="RECORD_HALL",
            action_status="IDLE", health_fp=1000, away_until_world_minute=None))
        submitted = self.run_action("request_dish_review", dict(instance_id=dish["instance_id"]), location="RECORD_HALL")
        self.assertTrue(submitted["success"], (submitted.get("result_code"), submitted.get("message")))
        review_id = submitted["data"]["result"]["review"]["review_id"]
        mark_sample = pending_reviews(mark, self.clock.world_minute, review_id)["reviews"][0]
        self.assertEqual(mark_sample["dish"]["ingredients"][0]["item_id"], 1030)
        self.assertEqual(mark_sample["dish"]["ingredients"][0]["amount"], 2)
        self.assertTrue(mark_sample["dish"]["ingredients"][0]["name"])
        rated = rate_dish("10013", dict(review_id=review_id, scores=dict(color=8, aroma=8, taste=8, presentation=8),
                                      comment="真实样品的玉米香气清楚。"), self.clock.world_minute, uuid.uuid4())
        self.assertEqual(rated["result_code"], "OK", rated.get("data"))
        claimed = self.run_action("claim_dish_review", dict(review_id=review_id))
        self.assertTrue(claimed["success"], (claimed.get("result_code"), claimed.get("message")))
        returned = self.bag_a.slots.get(instance_id=claimed["data"]["result"]["returned_instance_id"])
        self.assertEqual((returned.expires_world_minute, returned.rated), (dish["expires_world_minute"], True))
        commission = publish_commission(self.npc, dict(title="新鲜餐食供货", reward_gold=12,
            valid_for_minutes=720, default_location_code="RESTAURANT", stages=[dict(id="meal", conditions=[
                dict(id="food", type="item", item_ids=[29201], target=1, min_quality=2,
                     requires_freshness=True, min_remaining_minutes=60)])]), uuid.uuid4(), self.clock.world_minute)
        cid = commission["commission_id"]
        self.assertTrue(self.run_action("commission_accept", dict(commission_id=cid))["success"])
        delivered = self.run_action("commission_contribute", dict(commission_id=cid, condition_id="food",
            item_id=29201, quantity=1, instance_id=str(returned.instance_id)))
        self.assertTrue(delivered["success"], (delivered.get("result_code"), delivered.get("message")))
        received = self.lucy.slots.get(item_id=29201)
        self.assertEqual(received.expires_world_minute, dish["expires_world_minute"])
        self.assertEqual(received.dish_metadata["maker_player_id"], str(self.a.pk))
        self.assertNotIn("maker_npc_id", received.dish_metadata)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.a, skill_code="cooking").total_xp, 60)
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a, skill_code="cooking").count(), 2)
        self.assertTrue(PlayerMilestoneProgress.objects.filter(player=self.a, code="first_formal_dish_review").exists())
