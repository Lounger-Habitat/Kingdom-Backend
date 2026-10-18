import copy
import importlib
import uuid
from unittest.mock import patch

from django.apps import apps
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from account.models import GameAccount
from inventory.models import (CollectionPoint, CollectionPointLevel, CollectionPointLevelItem,
                              PlayerCollectionPoint, PlayerCollectionOverride)
from items.models import Item
from player.models import (ItemGameplayDefinition, PlayerGrowthState, PlayerMilestoneProgress,
                           PlayerSkillProgress, PlayerState, SkillRewardReceipt)
from player.progression import award_skill, ensure_growth, growth_snapshot, settle_growth
from player.progression_rules import LEVEL_COSTS, LEVEL_TOTALS, MAX_XP, SKILLS, skill_data
from village.models import VillagePlayerActionReceipt
from village.player_actions import execute_player_action
from village.tests.test_player_actions import PlayerFixture


class GrowthRuleTests(SimpleTestCase):
    def test_level_boundaries_and_max_have_nonnegative_level_progress(self):
        for i, floor in enumerate(LEVEL_TOTALS):
            state = skill_data("gathering", "采集", floor)
            self.assertEqual((state["level"], state["xp_in_level"]), (i + 1, 0))
            self.assertEqual(state["xp_to_next_level"], LEVEL_COSTS[i] if i < 9 else 0)
            if i:
                self.assertEqual(skill_data("gathering", "采集", floor - 1)["level"], i)
        self.assertEqual(skill_data("gathering", "采集", MAX_XP + 20)["total_xp"], MAX_XP)


class GrowthServiceTests(TestCase):
    def setUp(self):
        self.player = GameAccount.objects.create(username="growth-service")

    def test_six_skills_persist_and_only_released_routes_are_live(self):
        first = growth_snapshot(self.player)
        self.assertEqual([s["code"] for s in first["skills"]], [code for code, _ in SKILLS])
        self.assertEqual(PlayerSkillProgress.objects.filter(player=self.player).count(), 6)
        self.assertEqual(first, growth_snapshot(GameAccount.objects.get(pk=self.player.pk)))
        for skill in first["skills"]:
            self.assertEqual(skill["progression_enabled"], skill["code"] in {"gathering", "business"})
            for node in skill["unlocks"]:
                self.assertFalse(node["available"])
                if node["code"] == "targeted_gathering":
                    self.assertTrue(node["implemented"])
                    self.assertEqual(node["reason"], "SKILL_LEVEL_REQUIRED")
                else:
                    self.assertFalse(node["implemented"])
                    self.assertEqual(node["reason"], "CONTENT_NOT_RELEASED")

    def test_generic_award_cross_level_replay_and_account_isolation(self):
        other = GameAccount.objects.create(username="growth-other")
        source = str(uuid.uuid4())
        result = award_skill(self.player, source_type="trusted-future-harvest", source_id=source,
                             skill_code="farming", xp=450)
        self.assertEqual((result["level_before"], result["level_after"], result["total_xp"]), (1, 4, 450))
        revision = growth_snapshot(self.player)["revision"]
        self.assertEqual(result, award_skill(self.player, source_type="trusted-future-harvest",
                         source_id=source, skill_code="farming", xp=450))
        self.assertEqual(growth_snapshot(self.player)["revision"], revision)
        award_skill(other, source_type="trusted-future-harvest", source_id=source, skill_code="farming", xp=80)
        self.assertEqual(PlayerSkillProgress.objects.get(player=self.player, skill_code="farming").total_xp, 450)
        self.assertEqual(PlayerSkillProgress.objects.get(player=other, skill_code="farming").total_xp, 80)
        # Generic machinery is ready, but it does not open the future route.
        self.assertFalse(growth_snapshot(self.player)["skills"][1]["progression_enabled"])

    def test_first_real_source_bonus_once_and_snapshot_is_not_shared(self):
        source = str(uuid.uuid4())
        first = settle_growth(self.player, source_type="collect", source_id=source)
        self.assertEqual(first["xp_gained"], 30)
        self.assertEqual(first["milestones"][0]["xp_reward"], 20)
        self.assertEqual(first, settle_growth(self.player, source_type="collect", source_id=source))
        second = settle_growth(self.player, source_type="collect", source_id=str(uuid.uuid4()))
        self.assertEqual((second["xp_gained"], second["total_xp"], second["milestones"]), (10, 40, []))
        self.assertEqual(PlayerMilestoneProgress.objects.filter(player=self.player).count(), 1)
        first["milestones"][0]["title"] = "mutated"
        self.assertEqual(settle_growth(self.player, source_type="collect", source_id=source)["milestones"][0]["title"], "第一次采集")

    def test_cap_uses_actual_granted_xp_and_does_not_repeat(self):
        ensure_growth(self.player)
        PlayerSkillProgress.objects.filter(player=self.player, skill_code="gathering").update(total_xp=MAX_XP - 5)
        first = settle_growth(self.player, source_type="collect", source_id=str(uuid.uuid4()))
        self.assertEqual((first["xp_gained"], first["total_xp"], first["level_after"]), (5, MAX_XP, 10))
        self.assertEqual(first["milestones"][0]["xp_reward"], 0)
        second = settle_growth(self.player, source_type="collect", source_id=str(uuid.uuid4()))
        self.assertEqual(second["xp_gained"], 0)
        skill = growth_snapshot(self.player)["skills"][0]
        self.assertEqual((skill["xp_in_level"], skill["xp_to_next_level"]), (0, 0))

    def test_outer_failure_rolls_back_xp_receipt_milestone_and_initialization(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                settle_growth(self.player, source_type="collect", source_id=str(uuid.uuid4()))
                raise RuntimeError("asset settlement failed")
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.player).exists())
        self.assertFalse(SkillRewardReceipt.objects.filter(player=self.player).exists())
        self.assertFalse(PlayerMilestoneProgress.objects.filter(player=self.player).exists())

    def test_unreleased_source_and_invalid_xp_do_not_initialize_progress(self):
        with self.assertRaises(ValueError):
            settle_growth(self.player, source_type="hunting", source_id="kill")
        with self.assertRaises(ValueError):
            award_skill(self.player, source_type="collect", source_id="bad", skill_code="gathering", xp=True)
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.player).exists())

    def test_item_definition_is_static_and_does_not_claim_a_runtime_gate(self):
        item = Item.objects.create(item_id=30001, item_name="测试工具", max_use_count=50)
        ItemGameplayDefinition.objects.create(item=item, capability_code="gathering", tier=2,
                                               required_skill="gathering", required_level=4)
        data = next(row for row in growth_snapshot(self.player)["equipment_definitions"] if row["item_id"] == item.pk)
        self.assertEqual((data["item_id"], data["maximum_uses"], data["tier"]), (30001, 50, 2))
        self.assertFalse(data["runtime_enforced"])


class LegacyGrowthTests(TestCase):
    def setUp(self):
        self.food = CollectionPoint.objects.get(collect_type="采集食材")
        self.meat = CollectionPoint.objects.get(collect_type="采集肉类")
        self.item = Item.objects.create(item_id=30002, item_name="历史覆盖材料")

    def legacy_player(self, level):
        player = GameAccount.objects.create(username=f"legacy-growth-{level}")
        point = PlayerCollectionPoint.objects.create(player=player, collection_point=self.food, current_level=level)
        PlayerCollectionPoint.objects.create(player=player, collection_point=self.meat, current_level=5)
        PlayerCollectionOverride.objects.create(player_collection_point=point, item=self.item,
                                                override_min_amount=17, override_max_amount=21)
        return player, point

    def test_food_floor_migration_preserves_source_pools_overrides_and_meat_history(self):
        for old, expected in [(1, 1), (2, 2), (3, 4), (4, 6), (5, 7)]:
            player, point = self.legacy_player(old)
            before = list(PlayerCollectionOverride.objects.filter(player_collection_point=point).values())
            first = growth_snapshot(player)
            self.assertEqual(first["skills"][0]["level"], expected)
            self.assertEqual(first["skills"][3]["total_xp"], 0)
            self.assertEqual(first, growth_snapshot(player))
            point.refresh_from_db()
            self.assertEqual(point.current_level, old)
            self.assertEqual(before, list(PlayerCollectionOverride.objects.filter(player_collection_point=point).values()))
            self.assertEqual(len(first["legacy_entitlements"]["collection_points"]), 2)
            self.assertFalse(SkillRewardReceipt.objects.filter(player=player).exists())
            self.assertFalse(PlayerMilestoneProgress.objects.filter(player=player).exists())

    def test_frozen_data_migration_is_idempotent_and_matches_lazy_migration(self):
        player, _ = self.legacy_player(4)
        migrate = importlib.import_module("player.migrations.0014_player_growth").migrate_legacy_growth
        class Editor:
            connection = connection
        migrate(apps, Editor())
        first = growth_snapshot(player, read_only=True)
        self.assertEqual(first["skills"][0]["level"], 6)
        migrate(apps, Editor())
        self.assertEqual(first, growth_snapshot(player))
        self.assertEqual(PlayerSkillProgress.objects.filter(player=player).count(), 6)


class GrowthCollectionIntegrationTests(PlayerFixture, TestCase):
    def setUp(self):
        super().setUp()
        point = CollectionPoint.objects.get(collect_type="采集食材")
        point.action_cost, point.collect_count = 15, 1
        point.save(update_fields=["action_cost", "collect_count"])
        level, _ = CollectionPointLevel.objects.get_or_create(collection_point=point, level=1)
        level.items.all().delete()
        CollectionPointLevelItem.objects.create(level=level, item_id=1030, collect_min_amount=2, collect_max_amount=2)

    def test_real_collect_asset_ap_growth_receipt_replay_and_conflict(self):
        payload = self.payload("collect", dict(collect_type="采集食材"))
        first = execute_player_action(payload)
        self.assertTrue(first["success"], first)
        self.assertEqual(first["data"]["result"]["growth_rewards"][0]["xp_gained"], 30)
        self.assertEqual(first, execute_player_action(copy.deepcopy(payload)))
        self.assertEqual(PlayerState.objects.get(player_id=str(self.a.pk)).action_points, 185)
        self.assertEqual(self.bag_a.slots.get().item_amount, 2)
        self.assertEqual(SkillRewardReceipt.objects.filter(player=self.a).count(), 1)
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.b).exists())
        conflict = copy.deepcopy(payload)
        conflict["action"], conflict["params"] = "overview", {}
        self.assertEqual(execute_player_action(conflict)["result_code"], "REQUEST_CONFLICT")
        current = self.run_action("overview")["data"]["overview"]["growth"]
        self.assertEqual(current, first["data"]["overview"]["growth"])

    def test_failed_collect_and_receipt_failure_never_leave_growth_or_assets(self):
        state = PlayerState.objects.create(player_id=str(self.a.pk), action_points=0)
        failure = self.run_action("collect", dict(collect_type="采集食材"))
        self.assertEqual(failure["result_code"], "INSUFFICIENT_ACTION_POINTS")
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.a).exists())
        state.action_points = 200
        state.save(update_fields=["action_points"])
        with patch("village.player_actions.VillagePlayerActionReceipt.objects.create", side_effect=RuntimeError("receipt failed")):
            with self.assertRaises(RuntimeError):
                self.run_action("collect", dict(collect_type="采集食材"))
        state.refresh_from_db()
        self.assertEqual(state.action_points, 200)
        self.assertFalse(self.bag_a.slots.exists())
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.a).exists())
        self.assertFalse(SkillRewardReceipt.objects.filter(player=self.a).exists())
        self.assertFalse(PlayerMilestoneProgress.objects.filter(player=self.a).exists())

    def test_observer_overview_is_read_only_even_before_legacy_migration(self):
        point = CollectionPoint.objects.get(collect_type="采集食材")
        PlayerCollectionPoint.objects.create(player=self.a, collection_point=point, current_level=3)
        payload = dict(self.payload("overview"), read_only=True)
        with CaptureQueriesContext(connection) as queries:
            answer = execute_player_action(payload)
        self.assertTrue(answer["success"], answer)
        self.assertEqual(answer["data"]["overview"]["growth"]["skills"][0]["level"], 4)
        writes = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        self.assertFalse(PlayerGrowthState.objects.filter(player=self.a).exists())
        self.assertFalse(PlayerSkillProgress.objects.filter(player=self.a).exists())
        self.assertFalse(VillagePlayerActionReceipt.objects.filter(player=self.a).exists())
