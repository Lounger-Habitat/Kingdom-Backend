import copy
import uuid
from unittest.mock import patch

from django.test import TestCase

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import InventoryBag, InventorySlot
from items.models import Item
from player.models import PlayerSkillProgress, PlayerState, SkillRewardReceipt
from village.models import VillageFarmPlot, VillageSimulationConfig
from village.phase3_catalog import FARM_CROP_RULES
from village.player_actions import execute_player_action
from village.player_farm import farm_overview


class PlayerFarmGrowthTests(TestCase):
    def test_admin_configured_extra_harvest_is_frozen_at_planting(self):
        from unittest.mock import Mock
        from player.models import EquipmentLevel
        self.level(80)
        self.tool.equipment_level = 2
        self.tool.save(update_fields=["equipment_level"])
        rule = EquipmentLevel.objects.get(equipment__item_id=4001, level=2)
        rule.harvest_bonus_chance = 1
        rule.save()
        rng = Mock()
        rng.randint.return_value = 4
        rng.random.return_value = 0
        with patch("village.player_farm.deterministic_rng", return_value=rng):
            answer = self.act("plant_farm_plot", crop_code="CORN")
        self.assertTrue(answer["success"], answer)
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.production_rules_snapshot["tool_harvest_bonus_chance"], 1)
        self.assertEqual(self.plot.production_rules_snapshot["yield_amount"], 5)
        rule.harvest_bonus_chance = 0
        rule.save()
        self.mature()
        answer = self.act("harvest_farm_plot", expected_batch_id=str(self.plot.batch_id))
        self.assertTrue(answer["success"], answer)
        self.assertEqual(self.bag.slots.get(item_id=1030).item_amount, 5)

    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults=dict(village_economy_enabled=True))
        self.clock = GlobalGameTimeState.get_instance(); self.clock.world_minute = 100; self.clock.save()
        self.player = GameAccount.objects.create(username="growth-farmer")
        self.bag = InventoryBag.objects.create(player=self.player, character_id=self.player.username, money=300)
        PlayerState.objects.create(player_id=str(self.player.pk), action_points=200)
        self.plot = VillageFarmPlot.objects.create(plot_code="G2_TEST_PLOT", lease_enabled=True,
            lessee_player=self.player, leased_world_minute=100, lease_ends_world_minute=20000)
        for key in (1051, 1030, 1038, 1043):
            Item.objects.get_or_create(item_id=key, defaults=dict(item_name=str(key), item_price=5))
        self.tool = self.slot(4001, 1, 20)
        self.slot(1051, 3)

    def slot(self, item_id, amount, uses=None):
        return InventorySlot.objects.create(bag=self.bag, slot_index=self.bag.slots.count(), item_id=item_id,
                                            item_amount=amount, remaining_uses=uses)

    def level(self, xp):
        PlayerSkillProgress.objects.update_or_create(player=self.player, skill_code="farming", defaults=dict(total_xp=xp))

    def act(self, action, **params):
        self.plot.refresh_from_db()
        payload = dict(player_id=str(self.player.pk), request_id=str(uuid.uuid4()), action=action,
            params=dict(plot_code=self.plot.plot_code, expected_plot_version=self.plot.plot_version, **params),
            current_location_code="BILL_FARM")
        return execute_player_action(payload)

    def mature(self):
        self.plot.refresh_from_db()
        self.clock.world_minute = self.plot.mature_world_minute
        self.clock.save(update_fields=["world_minute"])

    def test_new_batch_freezes_yield_quality_seeds_time_and_xp_across_growth_and_tuning(self):
        self.level(80)
        first = self.act("plant_farm_plot", crop_code="CORN")
        self.assertTrue(first["success"], first)
        self.plot.refresh_from_db()
        frozen, batch = copy.deepcopy(self.plot.production_rules_snapshot), str(self.plot.batch_id)
        self.assertIn(frozen["quality"], range(1, 7))
        self.assertEqual((frozen["bonus_seed_chance"], frozen["effective_growth_minutes"]), (.30, 1440))
        self.level(780)
        self.mature()
        altered = dict(FARM_CROP_RULES["CORN"], output_item_id=1043, seed_item_id=29305,
                       yield_min=100, yield_max=100, bonus_seed_chance=1, growth_minutes=1)
        with patch.dict(FARM_CROP_RULES, {"CORN": altered}):
            result = self.act("harvest_farm_plot", expected_batch_id=batch)
        self.assertTrue(result["success"], result)
        output = self.bag.slots.get(item_id=1030)
        self.assertEqual((output.item_amount, output.quality, output.expires_world_minute),
                         (frozen["yield_amount"], frozen["quality"], None))
        receipt = SkillRewardReceipt.objects.get(player=self.player, skill_code="farming")
        self.assertEqual((receipt.source_id, receipt.result["xp_gained"]), (batch, 40))
        expected = dict(crop_code="CORN", batch_id=batch, output_item_id=1030, amount=frozen["yield_amount"], quality=frozen["quality"])
        self.assertEqual({key: receipt.result["facts"][key] for key in expected}, expected)
        self.assertEqual(receipt.result["facts"]["tool_equipment_level"], 1)
        self.assertFalse(self.bag.slots.filter(item_id=1043).exists())
        self.assertIsNone(self.bag.slots.filter(item_id=1051).order_by("-slot_index").first().expires_world_minute)

    def test_legacy_batch_has_old_quality_no_freshness_and_only_one_real_harvest_reward(self):
        self.plot.status, self.plot.crop_code = "GROWING", "CORN"
        self.plot.batch_id, self.plot.planted_world_minute, self.plot.mature_world_minute = uuid.uuid4(), 0, 100
        self.plot.yield_seed = 5
        self.plot.save()
        batch = str(self.plot.batch_id)
        self.level(780)
        result = self.act("harvest_farm_plot", expected_batch_id=batch)
        self.assertTrue(result["success"], result)
        output = self.bag.slots.get(item_id=1030)
        self.assertEqual((output.item_amount, output.quality, output.expires_world_minute), (5, 3, None))
        again = self.act("harvest_farm_plot", expected_batch_id=batch)
        self.assertFalse(again["success"])
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.player, skill_code="farming").count(), 1)

    def test_new_crops_require_skill_and_t2_and_produce_quality_four_at_level_five(self):
        self.slot(29304, 2)
        low = self.act("plant_farm_plot", crop_code="ONION")
        self.assertEqual(low["result_code"], "SKILL_LEVEL_REQUIRED")
        self.level(780)
        no_tool = self.act("plant_farm_plot", crop_code="ONION")
        self.assertEqual(no_tool["result_code"], "TOOL_TIER_REQUIRED")
        better = self.slot(4103, 1, 50)
        better.equipment_level = 3; better.save(update_fields=["equipment_level"])
        planted = self.act("plant_farm_plot", crop_code="ONION", tool_instance_id=str(better.instance_id))
        self.assertTrue(planted["success"], planted)
        self.plot.refresh_from_db()
        frozen_quality = self.plot.production_rules_snapshot["quality"]
        self.assertIn(frozen_quality, range(1, 7))
        self.assertEqual(self.plot.production_rules_snapshot["growth_minutes"], 720)
        self.mature()
        harvested = self.act("harvest_farm_plot", expected_batch_id=str(self.plot.batch_id), tool_instance_id=str(better.instance_id))
        self.assertTrue(harvested["success"], harvested)
        self.assertEqual(self.bag.slots.get(item_id=1038).quality, frozen_quality)
        catalog = farm_overview(self.player, self.bag, self.clock.world_minute)["crops"]
        self.assertEqual((catalog["ONION"]["growth_minutes"], catalog["GREEN_RADISH"]["growth_minutes"]), (720, 2160))
