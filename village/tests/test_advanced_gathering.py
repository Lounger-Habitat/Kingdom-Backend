import copy
import uuid
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from account.models import GameAccount
from game_time.models import GlobalGameTimeState
from inventory.models import (CollectionPoint, CollectionPointLevel, CollectionPointLevelItem,
    InventoryBag, InventorySlot, PlayerCollectionOverride, PlayerCollectionPoint)
from items.models import Item
from player.models import PlayerGrowthState, PlayerSkillProgress, PlayerState, SkillRewardReceipt
from village.advanced_gathering import COLLECT_TYPE, LOCATION, CAPACITY
from village.models import VillageGatheringResourceState, VillageSimulationConfig
from village.player_actions import execute_player_action


class AdvancedGatheringIntegrationTests(TestCase):
    def setUp(self):
        VillageSimulationConfig.objects.update_or_create(config_key="default", defaults=dict(village_economy_enabled=True))
        self.clock = GlobalGameTimeState.get_instance()
        self.clock.world_minute = 100
        self.clock.save()
        self.players, self.bags, self.tools = [], [], []
        for name in ("undergrowth-alpha", "undergrowth-beta"):
            player = GameAccount.objects.create(username=name)
            bag = InventoryBag.objects.create(player=player, character_id=name, money=200)
            PlayerState.objects.create(player_id=str(player.pk), action_points=200)
            PlayerSkillProgress.objects.create(player=player, skill_code="gathering", total_xp=440)
            tool = InventorySlot.objects.create(bag=bag, slot_index=0, item_id=4102, item_amount=1, remaining_uses=50)
            self.players.append(player); self.bags.append(bag); self.tools.append(tool)
        self.point, _ = CollectionPoint.objects.update_or_create(collect_type=COLLECT_TYPE,
            defaults=dict(name="林下食材点", item_type="食材", action_cost=20, collect_count=2, default_level=1))
        level, _ = CollectionPointLevel.objects.get_or_create(collection_point=self.point, level=1)
        level.items.all().delete()
        for item_id in (1035, 1038):
            item, _ = Item.objects.get_or_create(item_id=item_id, defaults=dict(item_name=str(item_id), item_price=4))
            CollectionPointLevelItem.objects.create(level=level, item=item, collect_weight=100,
                                                    collect_min_amount=2, collect_max_amount=4)

    def payload(self, index=0, *, rid=None, tool=None):
        return dict(player_id=str(self.players[index].pk), request_id=str(rid or uuid.uuid4()), action="collect",
            params=dict(collect_type=COLLECT_TYPE, tool_instance_id=str((tool or self.tools[index]).instance_id)),
            current_location_code=LOCATION)

    def resource(self, amount, restore=1540):
        return VillageGatheringResourceState.objects.create(code=LOCATION, remaining_quantity=amount,
                                                            next_restore_world_minute=restore)

    def state(self, index=0):
        tool = InventorySlot.objects.get(pk=self.tools[index].pk)
        resource = VillageGatheringResourceState.objects.get(pk=LOCATION)
        return (PlayerState.objects.get(player_id=str(self.players[index].pk)).action_points,
            tool.remaining_uses, PlayerSkillProgress.objects.get(player=self.players[index], skill_code="gathering").total_xp,
            resource.remaining_quantity, resource.revision,
            sum(self.bags[index].slots.filter(item_id__in=[1035, 1038]).values_list("item_amount", flat=True)),
            SkillRewardReceipt.objects.filter(player=self.players[index]).count())

    def test_skill_four_and_actual_t2_are_required_before_any_charge(self):
        self.resource(120)
        PlayerSkillProgress.objects.filter(player=self.players[0], skill_code="gathering").update(total_xp=220)
        before = self.state()
        low = execute_player_action(self.payload())
        self.assertEqual(low["result_code"], "SKILL_LEVEL_REQUIRED")
        self.assertEqual(self.state(), before)
        PlayerSkillProgress.objects.filter(player=self.players[0], skill_code="gathering").update(total_xp=440)
        t1 = InventorySlot.objects.create(bag=self.bags[0], slot_index=1, item_id=4101, item_amount=1, remaining_uses=20)
        before = self.state()
        wrong_tier = execute_player_action(self.payload(tool=t1))
        self.assertEqual(wrong_tier["result_code"], "TOOL_TIER_REQUIRED")
        self.assertEqual(self.state(), before)
        foreign = execute_player_action(self.payload(tool=self.tools[1]))
        self.assertEqual(foreign["result_code"], "TOOL_MISSING")
        self.assertEqual(self.state(), before)

    def test_two_players_share_final_units_without_overproduction(self):
        self.resource(6)
        with patch("village.player_actions.random.randint", return_value=2):
            first = execute_player_action(self.payload(0))
            second = execute_player_action(self.payload(1))
        self.assertTrue(first["success"], first)
        self.assertTrue(second["success"], second)
        quantities = [sum(b["quantity"] for b in answer["data"]["result"]["collected_items"])
                      for answer in (first, second)]
        self.assertEqual(quantities, [4, 2])
        self.assertEqual(VillageGatheringResourceState.objects.get(pk=LOCATION).remaining_quantity, 0)
        for index in (0, 1):
            self.assertEqual(self.state(index)[:3], (180, 49, 475))  # 15 XP + one-time 20 XP.

    def test_replay_never_recharges_or_rewinds_shared_stock_after_other_player(self):
        self.resource(20)
        payload = self.payload(0)
        with patch("village.player_actions.random.randint", return_value=2):
            first = execute_player_action(payload)
            self.assertTrue(execute_player_action(self.payload(1))["success"])
        before = self.state(0)
        self.assertEqual(execute_player_action(copy.deepcopy(payload)), first)
        self.assertEqual(self.state(0), before)
        self.assertEqual(before[:3], (180, 49, 475))
        self.assertEqual(before[3], 12)
        self.assertEqual(before[-1], 1)

    def test_depleted_action_leaves_ap_tool_inventory_and_xp_unchanged(self):
        self.resource(0)
        before = self.state()
        payload = self.payload()
        failure = execute_player_action(payload)
        self.assertEqual(failure["result_code"], "RESOURCE_DEPLETED")
        self.assertEqual(self.state(), before)
        self.assertEqual(execute_player_action(copy.deepcopy(payload)), failure)
        self.assertEqual(self.state(), before)

    def test_observer_virtual_recovery_projects_full_stock_without_any_write(self):
        resource = self.resource(0, restore=1540)
        self.clock.world_minute = 2000
        self.clock.save(update_fields=["world_minute"])
        payload = dict(player_id=str(self.players[0].pk), request_id=str(uuid.uuid4()), action="overview",
                       params={}, current_location_code="", read_only=True)
        with CaptureQueriesContext(connection) as queries:
            answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer)
        point = next(p for p in answer["data"]["overview"]["collection_points"] if p["collect_type"] == COLLECT_TYPE)
        self.assertEqual(point["remaining_quantity"], CAPACITY)
        self.assertGreater(point["next_restore_world_minute"], 2000)
        resource.refresh_from_db()
        self.assertEqual((resource.remaining_quantity, resource.next_restore_world_minute), (0, 1540))
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.players[0]).exists())

    def test_effective_personal_override_cannot_be_bypassed_by_advanced_point(self):
        self.resource(20)
        owned = PlayerCollectionPoint.objects.create(player=self.players[0], collection_point=self.point, current_level=1)
        PlayerCollectionOverride.objects.create(player_collection_point=owned, item_id=1035, is_excluded=True)
        result = execute_player_action(self.payload())
        self.assertTrue(result["success"], result)
        self.assertEqual({b["item_id"] for b in result["data"]["result"]["collected_items"]}, {1038})
