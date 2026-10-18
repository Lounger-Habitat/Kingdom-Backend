from unittest.mock import patch
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from account.models import GameAccount
from cooking.models import PityCounter
from game_time.models import GlobalGameTimeState
from game_time.views import season_refresh
from inventory.components import BATCH_COPY_FIELDS
from inventory.models import InventoryBag, InventorySlot
from player.models import PlayerRecipe, PlayerSkillProgress, PlayerState
from recipes.models import Recipe
from restaurant.models import MenuItem, PlayerRestaurantState
from season.models import Season
from season.season_service import force_transition_remaining_players
from village.models import VillageSimulationConfig


class VillageSeasonCompatibilityTests(TestCase):
    def test_public_and_scheduled_transition_preserve_village_batches_without_refilling_ap(self):
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults=dict(village_economy_enabled=True))
        GlobalGameTimeState.get_instance()
        player = GameAccount.objects.create(username="village-season")
        now = timezone.now()
        active = Season.objects.create(season_id="compat-active", sequence=999, start_time=1,
                                       end_time=9999999999, status="active")
        state = PlayerState.objects.create(player_id=str(player.pk), current_season_id="compat-old", action_points=17,
            action_points_recovered_at=now-timezone.timedelta(seconds=181))
        bag = InventoryBag.objects.create(player=player, character_id=player.username, money=123)
        escrow = InventoryBag.objects.create(player=player, character_id=f"stall:{player.pk}")
        raw = InventorySlot.objects.create(bag=bag, slot_index=0, item_id=1002, item_amount=4)
        held = InventorySlot.objects.create(bag=escrow, slot_index=0, item_id=29201, item_amount=2,
            produced_world_minute=10, expires_world_minute=2890, source_request_id=uuid.uuid4(),
            dish_metadata={"production_batch_id": str(uuid.uuid4())}, quality=4, ingredient="玉米×2")
        snapshot = {field: getattr(held, field) for field in BATCH_COPY_FIELDS}
        facility = PlayerRestaurantState.objects.create(player=player, current_level=4, stall_opened_world_minute=10)
        menu = MenuItem.objects.create(player=player, instance_id=str(held.instance_id), item_id=held.item_id,
            item_name="真实料理", quantity=2, batch_slot=held, listed_world_minute=10)
        recipe = Recipe.objects.create(item_id=29201, recipe_name="旧知识")
        knowledge = PlayerRecipe.objects.create(player=state, recipe_name=recipe.recipe_name,
                                                 catalog_recipe=recipe, show_ingredient_count=False)
        pity = PityCounter.objects.create(player=player, count_6=89)
        progress = PlayerSkillProgress.objects.create(player=player, skill_code="cooking", total_xp=440)
        with patch("player.services.timezone.now", return_value=now):
            for _ in range(2):
                request = APIRequestFactory().post("/api/game/time/season-refresh", {}, format="json")
                force_authenticate(request, user=player)
                self.assertTrue(season_refresh(request).data["success"])
            state.refresh_from_db()
            self.assertEqual((state.current_season_id, state.action_points), (active.season_id, 18))
            self.assertEqual(state.action_points_recovered_at, now-timezone.timedelta(seconds=1))
            # The scheduler calls the same service, with the same no-reset policy.
            state.current_season_id = "compat-old"; state.save(update_fields=["current_season_id"])
            force_transition_remaining_players("compat-old")
        state.refresh_from_db(); bag.refresh_from_db(); raw.refresh_from_db(); held.refresh_from_db()
        facility.refresh_from_db(); menu.refresh_from_db(); knowledge.refresh_from_db(); pity.refresh_from_db(); progress.refresh_from_db()
        self.assertEqual((state.action_points, bag.money, raw.item_amount, facility.current_level, menu.quantity), (18, 123, 4, 4, 2))
        self.assertEqual({field: getattr(held, field) for field in BATCH_COPY_FIELDS}, snapshot)
        self.assertEqual((knowledge.show_ingredient_count, pity.count_6, progress.total_xp), (False, 89, 440))
