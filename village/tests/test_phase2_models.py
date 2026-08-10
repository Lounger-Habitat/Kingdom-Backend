import importlib
from io import StringIO

from django.apps import apps as global_apps
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.core.management import call_command

from game_time.models import GlobalGameTimeState
from inventory.models import NpcDefinition, NpcGlobalInventory
from items.models import Item
from village.models import (
    NpcNeedsDaySummary,
    NpcRuntimeState,
    NpcSocialDailyCounter,
    VillageCommunityMealDailyUsage,
)
from village.seed_data import NPC_SEEDS


class PhaseTwoModelConstraintTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.low_npc = NpcDefinition.objects.create(agent_id="P2-LOW", name="low")
        cls.high_npc = NpcDefinition.objects.create(agent_id="P2-HIGH", name="high")
        cls.runtime = NpcRuntimeState.objects.create(npc=cls.low_npc)

    def test_runtime_defaults_are_the_frozen_fixture_values(self):
        self.assertEqual(
            (
                self.runtime.hunger_fp,
                self.runtime.energy_fp,
                self.runtime.health_fp,
                self.runtime.social_fp,
                self.runtime.stress_fp,
                self.runtime.needs_world_minute,
            ),
            (200, 800, 1000, 200, 100, 0),
        )
        self.assertEqual(
            (
                self.runtime.hunger_remainder,
                self.runtime.energy_remainder,
                self.runtime.health_remainder,
                self.runtime.social_remainder,
                self.runtime.stress_remainder,
            ),
            (0, 0, 0, 0, 0),
        )

    def test_fixed_point_and_remainder_constraints_apply_in_sqlite(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            NpcRuntimeState.objects.filter(pk=self.runtime.pk).update(hunger_fp=1001)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NpcRuntimeState.objects.filter(pk=self.runtime.pk).update(stress_remainder=-30)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NpcRuntimeState.objects.filter(pk=self.runtime.pk).update(needs_world_minute=-1)

    def test_daily_summary_pair_and_community_quota_constraints_apply_in_sqlite(self):
        NpcNeedsDaySummary.objects.create(npc=self.low_npc, day_index=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NpcNeedsDaySummary.objects.create(npc=self.low_npc, day_index=0)
        NpcSocialDailyCounter.objects.create(
            low_npc=self.low_npc,
            high_npc=self.high_npc,
            day_index=0,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            NpcSocialDailyCounter.objects.create(
                low_npc=self.low_npc,
                high_npc=self.high_npc,
                day_index=0,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            VillageCommunityMealDailyUsage.objects.create(day_index=0, total_meals=6)


class PhaseTwoSeedMigrationTests(TestCase):
    migration_module = importlib.import_module("village.migrations.0007_seed_phase2_food")
    cursor_migration_module = importlib.import_module("village.migrations.0006_phase2_needs_models")

    @classmethod
    def setUpTestData(cls):
        for seed in NPC_SEEDS:
            for item_id, _amount in seed["items"]:
                Item.objects.get_or_create(
                    item_id=item_id,
                    defaults={"item_name": f"seed-{item_id}", "item_type": "Commodity"},
                )

    def test_empty_migrated_database_then_seed_creates_runtime_and_rations_idempotently(self):
        formal_ids = [seed["agent_id"] for seed in NPC_SEEDS]
        # The migration chain may include legacy formal definitions in a
        # project-level test database.  Remove only these disposable test rows
        # to exercise the fresh migrate -> seed path deterministically.
        NpcDefinition.objects.filter(agent_id__in=formal_ids).delete()
        self.assertEqual(NpcDefinition.objects.filter(agent_id__in=formal_ids).count(), 0)
        call_command("seed_village_v1", stdout=StringIO())

        runtimes = NpcRuntimeState.objects.filter(npc__is_test_only=False).order_by("npc__agent_id")
        self.assertEqual(runtimes.count(), 5)
        self.assertTrue(all(runtime.hunger_fp == 200 for runtime in runtimes))
        self.assertTrue(all(runtime.energy_fp == 800 for runtime in runtimes))
        self.assertTrue(all(runtime.health_fp == 1000 for runtime in runtimes))
        cutover_minute = GlobalGameTimeState.get_instance().world_minute
        self.assertTrue(all(runtime.needs_world_minute == cutover_minute for runtime in runtimes))
        self.assertEqual(Item.objects.get(pk=29001).item_type, "食物")
        self.assertEqual(
            sum(
                bag.slots.filter(item_id=29001, item_amount=1).count()
                for bag in NpcGlobalInventory.objects.filter(npc__is_test_only=False)
            ),
            20,
        )

        runtime = runtimes.first()
        runtime.hunger_fp = 777
        runtime.needs_world_minute = 120
        runtime.save(update_fields=["hunger_fp", "needs_world_minute"])
        call_command("seed_village_v1", stdout=StringIO())
        runtime.refresh_from_db()
        self.assertEqual((runtime.hunger_fp, runtime.needs_world_minute), (777, 120))
        self.assertEqual(
            sum(
                bag.slots.filter(item_id=29001).count()
                for bag in NpcGlobalInventory.objects.filter(npc__is_test_only=False)
            ),
            20,
        )

    def test_data_migration_is_idempotent_and_reverse_keeps_consumed_seed(self):
        npcs = [
            NpcDefinition.objects.get_or_create(
                agent_id=agent_id,
                defaults={"name": f"npc-{agent_id}"},
            )[0]
            for agent_id in ("10011", "10012", "10013", "10014", "10015")
        ]
        for npc in npcs:
            NpcGlobalInventory.objects.get_or_create(npc=npc)

        self.migration_module.seed_phase2_food(global_apps, None)
        self.migration_module.seed_phase2_food(global_apps, None)
        self.assertEqual(
            sum(bag.slots.filter(item_id=29001).count() for bag in NpcGlobalInventory.objects.all()),
            20,
        )

        consumed = NpcGlobalInventory.objects.get(npc__agent_id="10011").slots.filter(item_id=29001).first()
        consumed.item_amount = 0
        consumed.save(update_fields=["item_amount"])
        self.migration_module.reverse_phase2_food(global_apps, None)
        self.assertEqual(
            NpcGlobalInventory.objects.get(npc__agent_id="10011").slots.filter(item_id=29001).count(),
            1,
        )
        self.assertEqual(
            sum(bag.slots.filter(item_id=29001).count() for bag in NpcGlobalInventory.objects.all()),
            1,
        )

    def test_data_migration_rejects_an_incompatible_existing_food_id(self):
        Item.objects.filter(pk=29001).delete()
        Item.objects.create(item_id=29001, item_name="not-food", item_type="Commodity")

        with self.assertRaisesRegex(RuntimeError, "incompatible definition"):
            self.migration_module.seed_phase2_food(global_apps, None)

    def test_cutover_migration_sets_existing_runtime_cursor_to_authority_minute(self):
        npc = NpcDefinition.objects.create(agent_id="P2-CUTOVER", name="cutover")
        runtime = NpcRuntimeState.objects.create(npc=npc, needs_world_minute=0)
        time_state = GlobalGameTimeState.get_instance()
        time_state.world_minute = 9876
        time_state.save(update_fields=["world_minute"])
        self.cursor_migration_module.initialize_needs_cursor_at_cutover(global_apps, None)
        runtime.refresh_from_db()
        self.assertEqual(runtime.needs_world_minute, 9876)
