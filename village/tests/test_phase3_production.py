"""Tests for Phase 3 production actions: PLANT, HARVEST, HUNT, COOK.

Covers success, wrong NPC, wrong location, missing items, tool broken,
plot not empty/ready, idempotent replay, and concurrent operations.
"""

import time
import uuid

from django.test import TestCase

from game_time.models import GlobalGameTimeState
from inventory.models import (
    NpcDefinition,
    NpcGlobalInventory,
    NpcGlobalInventorySlot,
)
from recipes.models import Recipe, RecipeIngredient
from village.models import (
    NpcProfile,
    NpcRuntimeState,
    VillageActionReceipt,
    VillageFarmPlot,
    VillageSimulationConfig,
)
from village.phase3_asset_service import commit_economy_action
from village.phase3_catalog import CORN_GROWTH_MINUTES, HUNT_MEAT_ITEM_IDS
from village.phase3_contract import (
    VILLAGE_ECONOMY_ACTION_V1,
    ResultCode,
)
from village.runtime_services import CommandService


def _payload(command_name, npc_id, location, params, request_id=None):
    return {
        "schema_version": VILLAGE_ECONOMY_ACTION_V1,
        "command_id": str(uuid.uuid4()),
        "request_id": str(request_id or uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "command_name": command_name,
        "npc_id": npc_id,
        "completed_world_minute": 700,
        "time_revision": 700,
        "current_location_code": location,
        "params": params,
    }


class ProductionTestBase(TestCase):
    """Shared setUp: config, time, NPCs, inventories, tools, seeds, plots."""

    def setUp(self):
        from items.models import Item
        Item.objects.get_or_create(item_id=1053, defaults={"item_name": "辣子鸡丁", "item_type": "食物", "item_price": 27, "sell_percentage": 1})
        VillageSimulationConfig.objects.create(
            config_key="default", village_economy_enabled=True,
        )
        self.time = GlobalGameTimeState.get_instance()
        self.time.world_minute = 700
        self.time.time_revision = 700
        self.time.save()

        # NPCs (get_or_create since migrations may pre-create them)
        self.bill, _ = NpcDefinition.objects.get_or_create(
            agent_id="10011", defaults={"name": "bill"},
        )
        self.jack, _ = NpcDefinition.objects.get_or_create(
            agent_id="10012", defaults={"name": "jack"},
        )
        self.mark, _ = NpcDefinition.objects.get_or_create(
            agent_id="10013", defaults={"name": "mark"},
        )
        self.lucy, _ = NpcDefinition.objects.get_or_create(
            agent_id="10014", defaults={"name": "lucy"},
        )
        self.nora, _ = NpcDefinition.objects.get_or_create(
            agent_id="10015", defaults={"name": "nora"},
        )

        # Inventories
        self.bill_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.bill, defaults={"money": 200},
        )
        self.jack_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.jack, defaults={"money": 240},
        )
        self.lucy_inv, _ = NpcGlobalInventory.objects.get_or_create(
            npc=self.lucy, defaults={"money": 300},
        )

        # Runtime states (for HUNT health gate)
        self.bill_rt, _ = NpcRuntimeState.objects.get_or_create(
            npc=self.bill,
            defaults={"current_location_code": "BILL_FARM", "health_fp": 1000},
        )
        self.jack_rt, _ = NpcRuntimeState.objects.get_or_create(
            npc=self.jack,
            defaults={"current_location_code": "HUNTING_GATE", "health_fp": 1000},
        )
        self.lucy_rt, _ = NpcRuntimeState.objects.get_or_create(
            npc=self.lucy,
            defaults={"current_location_code": "RESTAURANT", "health_fp": 1000},
        )

        # Farm plots
        for code in ("FARM_PLOT_A", "FARM_PLOT_B", "FARM_PLOT_C"):
            VillageFarmPlot.objects.get_or_create(
                plot_code=code, defaults={"status": "EMPTY"},
            )

        # Clear any migration-seeded slots for a clean test state
        NpcGlobalInventorySlot.objects.filter(
            bag__in=[self.bill_inv, self.jack_inv, self.lucy_inv],
        ).delete()

        # Tools for Bill (farm tool)
        self.bill_farm_tool = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=0, item_id=4001, item_amount=5,
            remaining_uses=20,
        )
        # Seeds for Bill
        self.bill_seeds = NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=1, item_id=1051, item_amount=10,
        )

        # Tool for Jack (hunt tool)
        self.jack_hunt_tool = NpcGlobalInventorySlot.objects.create(
            bag=self.jack_inv, slot_index=0, item_id=4002, item_amount=3,
            remaining_uses=15,
        )

    def _plant(self, **overrides):
        params = {
            "plot_code": "FARM_PLOT_A",
            "crop_code": "CORN",
            "expected_asset_version": self.bill_inv.asset_version,
        }
        params.update(overrides.pop("params", {}))
        return commit_economy_action(_payload(
            "PLANT", 10011, "BILL_FARM", params, **overrides,
        ))

    def _harvest(self, **overrides):
        params = {
            "plot_code": "FARM_PLOT_A",
            "expected_asset_version": self.bill_inv.asset_version,
        }
        params.update(overrides.pop("params", {}))
        return commit_economy_action(_payload(
            "HARVEST", 10011, "BILL_FARM", params, **overrides,
        ))

    def _hunt(self, **overrides):
        # Compatibility fixture: a formal HUNT accepted before animal production.
        from village.models import NpcCommandState
        params = {
            "hunt_code": "HUNT_CHICKEN",
            "expected_asset_version": self.jack_inv.asset_version,
        }
        params.update(overrides.pop("params", {}))
        payload = _payload(
            "HUNT", 10012, "HUNTING_GATE", params, **overrides,
        )
        cmd = NpcCommandState.objects.create(
            command_id=payload['command_id'], request_id=payload['request_id'], message_id=payload['message_id'],
            npc=self.jack, kind='HUNT', status='RUNNING', sanitized_parameters=params,
            started_world_minute=695, duration_minutes=5, production_state={},
        )
        self.jack_rt.active_command_id=cmd.command_id;self.jack_rt.needs_world_minute=695;self.jack_rt.save()
        return commit_economy_action(payload)

    def _cook(self, recipe_id=2, **overrides):
        params = {
            "recipe_id": recipe_id,
            "expected_asset_version": self.lucy_inv.asset_version,
        }
        params.update(overrides.pop("params", {}))
        return commit_economy_action(_payload(
            "COOK", 10014, "RESTAURANT", params, **overrides,
        ))


# ============================================================================
# PLANT tests
# ============================================================================


class PlantSuccessTest(ProductionTestBase):
    def test_plant_success(self):
        resp = self._plant()
        self.assertEqual(resp["result_code"], ResultCode.OK)
        self.assertIn("public_needs", resp["data"])

        # Seed consumed
        self.bill_seeds.refresh_from_db()
        self.assertEqual(self.bill_seeds.item_amount, 9)

        # Tool durability decreased
        self.bill_farm_tool.refresh_from_db()
        slots = list(self.bill_inv.slots.filter(item_id=4001))
        self.assertEqual(sum(row.item_amount for row in slots), 5)
        self.assertEqual(sum(row.item_amount * row.remaining_uses for row in slots), 99)

        # Plot created
        plot = VillageFarmPlot.objects.get(plot_code="FARM_PLOT_A")
        self.assertEqual(plot.status, "GROWING")
        self.assertIsNotNone(plot.batch_id)
        self.assertEqual(plot.mature_world_minute, 700 + CORN_GROWTH_MINUTES)
        self.assertIsNotNone(plot.yield_seed)

    def test_plant_uses_selected_crop_rule_on_independent_cell(self):
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=2, item_id=29301, item_amount=2,
        )
        resp = self._plant(params={
            "plot_code": "FARM_PLOT_A_02",
            "crop_code": "WHEAT",
        })

        self.assertEqual(resp["result_code"], ResultCode.OK)
        plot = VillageFarmPlot.objects.get(plot_code="FARM_PLOT_A_02")
        self.assertEqual(plot.crop_code, "WHEAT")
        self.assertEqual(resp["effects"]["plot"]["crop_code"], "WHEAT")
        self.assertEqual([(row["item_id"], row["amount"]) for row in resp["effects"]["items_consumed"]], [(29301, 1)])


class PlantFailureTest(ProductionTestBase):
    def test_plant_wrong_npc(self):
        resp = commit_economy_action(_payload(
            "PLANT", 10012, "BILL_FARM",
            {"plot_code": "FARM_PLOT_A", "crop_code": "CORN",
             "expected_asset_version": 0},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_FORBIDDEN_ACTOR)

    def test_plant_location_is_deferred_to_unity(self):
        resp = commit_economy_action(_payload(
            "PLANT", 10011, "VILLAGE_SQUARE",
            {"plot_code": "FARM_PLOT_A", "crop_code": "CORN",
             "expected_asset_version": 0},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)

    def test_plant_no_seeds(self):
        self.bill_seeds.item_amount = 0
        self.bill_seeds.save(update_fields=["item_amount"])
        resp = self._plant()
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)

    def test_plant_tool_broken(self):
        self.bill_farm_tool.remaining_uses = 0
        self.bill_farm_tool.save(update_fields=["remaining_uses"])
        resp = self._plant()
        self.assertEqual(resp["result_code"], ResultCode.P3_TOOL_BROKEN)

    def test_plant_plot_not_empty(self):
        VillageFarmPlot.objects.filter(plot_code="FARM_PLOT_A").update(status="GROWING")
        resp = self._plant()
        self.assertEqual(resp["result_code"], ResultCode.P3_PLOT_NOT_READY)


# ============================================================================
# HARVEST tests
# ============================================================================


class HarvestSuccessTest(ProductionTestBase):
    def test_harvest_success(self):
        # Plant first
        plant_resp = self._plant()
        self.assertEqual(plant_resp["result_code"], ResultCode.OK)
        planted_batch_id = plant_resp["effects"]["plot"]["batch_id"]

        # Advance time past maturity
        self.time.world_minute = 700 + CORN_GROWTH_MINUTES
        self.time.save(update_fields=["world_minute"])

        # Refresh asset version
        self.bill_inv.refresh_from_db()

        seeds_before = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.bill_inv, item_id=1051)
        )
        resp = self._harvest()
        self.assertEqual(resp["result_code"], ResultCode.OK)

        # Corn added (new slot created by add_batch)
        corn_slots = NpcGlobalInventorySlot.objects.filter(
            bag=self.bill_inv, item_id=1030,
        )
        total_corn = sum(s.item_amount for s in corn_slots)
        self.assertGreaterEqual(total_corn, 4)
        self.assertLessEqual(total_corn, 6)

        # Seeds added (new slot created by add_batch, total should increase)
        seeds_after = sum(
            s.item_amount for s in
            NpcGlobalInventorySlot.objects.filter(bag=self.bill_inv, item_id=1051)
        )
        self.assertGreaterEqual(seeds_after, seeds_before + 1)

        # Tool durability decreased (plant used 1, harvest used 1)
        self.bill_farm_tool.refresh_from_db()
        slots = list(self.bill_inv.slots.filter(item_id=4001))
        self.assertEqual(sum(row.item_amount for row in slots), 5)
        self.assertEqual(sum(row.item_amount * row.remaining_uses for row in slots), 98)

        # Plot cleared
        plot = VillageFarmPlot.objects.get(plot_code="FARM_PLOT_A")
        self.assertEqual(plot.status, "EMPTY")
        self.assertIsNone(plot.batch_id)
        harvested = resp["effects"]["plot"]
        self.assertEqual(harvested["harvested_crop_code"], "CORN")
        self.assertEqual(harvested["harvested_batch_id"], planted_batch_id)
        self.assertEqual(harvested["harvested_by_npc_id"], "10011")
        self.assertEqual(harvested["output"]["item_id"], 1030)


class HarvestFailureTest(ProductionTestBase):
    def test_harvest_too_early(self):
        self._plant()
        self.bill_inv.refresh_from_db()
        resp = self._harvest()
        self.assertEqual(resp["result_code"], ResultCode.P3_PLOT_NOT_READY)

    def test_harvest_wrong_npc(self):
        self._plant()
        self.time.world_minute = 700 + CORN_GROWTH_MINUTES
        self.time.save(update_fields=["world_minute"])

        resp = commit_economy_action(_payload(
            "HARVEST", 10012, "BILL_FARM",
            {"plot_code": "FARM_PLOT_A", "expected_asset_version": 0},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_FORBIDDEN_ACTOR)

    def test_harvest_location_is_deferred_to_unity(self):
        self._plant()
        self.time.world_minute = 700 + CORN_GROWTH_MINUTES
        self.time.save(update_fields=["world_minute"])
        self.bill_inv.refresh_from_db()

        resp = commit_economy_action(_payload(
            "HARVEST", 10011, "VILLAGE_SQUARE",
            {"plot_code": "FARM_PLOT_A",
             "expected_asset_version": self.bill_inv.asset_version},
        ))
        self.assertEqual(resp["result_code"], ResultCode.OK)


# ============================================================================
# HARVEST start checks
# ============================================================================


class HarvestStartTest(ProductionTestBase):
    def setUp(self):
        super().setUp()
        NpcProfile.objects.update_or_create(npc=self.bill, defaults={
            "display_name": "Bill", "occupation": "farmer",
            "home_location_code": "BILL_HOME", "work_location_code": "BILL_FARM",
            "social_location_code": "VILLAGE_SQUARE", "capabilities": ["HARVEST"],
        })
        self.plot = VillageFarmPlot.objects.get(plot_code="FARM_PLOT_A")
        self.plot.status = "GROWING"
        self.plot.crop_code = "CORN"
        self.plot.batch_id = uuid.uuid4()
        self.plot.planted_world_minute = 0
        self.plot.mature_world_minute = self.time.world_minute
        self.plot.save()

    def _accept_harvest(self, **params):
        self.bill_rt.refresh_from_db()
        ok, code, command = CommandService.accept_command(
            envelope_id=str(uuid.uuid4()), sender="10011", recipient="WuxiVillage",
            timestamp_ms=int(time.time() * 1000), content={
                "schema_version": "village.command.v1", "id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "name": "HARVEST",
                "params": {"plot_code": self.plot.plot_code, **params},
                "queue_mode": "STRICT", "expected_state_version": self.bill_rt.state_version,
                "not_before_world_minute": self.time.world_minute, "ttl_ms": 120000,
            },
        )
        self.assertTrue(ok, code)
        return command

    def _assert_start_rejected(self, code, failure_kind=None, **params):
        command = self._accept_harvest(**params)
        needs_before = (self.bill_rt.hunger_fp, self.bill_rt.energy_fp)
        ok, result, _ = CommandService.start_command(command.command_id)
        self.assertFalse(ok)
        self.assertEqual(result, code)
        command.refresh_from_db()
        self.bill_rt.refresh_from_db()
        self.assertEqual(command.status, "FAILED")
        self.assertIsNone(command.started_world_minute)
        self.assertEqual(command.completed_world_minute, self.time.world_minute)
        self.assertEqual(self.bill_rt.action_status, "IDLE")
        self.assertIsNone(self.bill_rt.active_command_id)
        self.assertIsNone(self.bill_rt.action_ends_world_minute)
        self.assertEqual((self.bill_rt.hunger_fp, self.bill_rt.energy_fp), needs_before)
        receipt = VillageActionReceipt.objects.get(request_id=command.request_id)
        self.assertFalse(receipt.success)
        if failure_kind:
            self.assertEqual(receipt.response_data["failure_kind"], failure_kind)

    def test_rejects_immature_crop_even_if_it_would_mature_during_action(self):
        self.plot.mature_world_minute = self.time.world_minute + 1
        self.plot.save()
        self._assert_start_rejected(ResultCode.P3_PLOT_NOT_READY, "PLOT_NOT_MATURE")
        self.bill_farm_tool.refresh_from_db()
        self.assertEqual(self.bill_farm_tool.remaining_uses, 20)

    def test_rejects_empty_plot_before_start(self):
        self.plot.status = "EMPTY"
        self.plot.save()
        self._assert_start_rejected(ResultCode.P3_PLOT_NOT_READY, "PLOT_EMPTY")

    def test_fast_integration_also_rejects_immature_crop_before_start(self):
        self.plot.mature_world_minute = self.time.world_minute + 1
        self.plot.save()
        with self.settings(VILLAGE_FAST_COMMAND_INTEGRATION=True):
            self._assert_start_rejected(ResultCode.P3_PLOT_NOT_READY, "PLOT_NOT_MATURE")

    def test_rejects_missing_broken_unknown_and_invalid_tools_before_start(self):
        for amount, uses in ((0, 20), (5, 0), (5, None), (5, -1)):
            with self.subTest(amount=amount, uses=uses):
                self.bill_farm_tool.item_amount = amount
                self.bill_farm_tool.remaining_uses = uses
                self.bill_farm_tool.save()
                self._assert_start_rejected(ResultCode.P3_TOOL_BROKEN, "FARM_TOOL_UNAVAILABLE")
                self.bill_farm_tool.refresh_from_db()
                self.assertEqual(self.bill_farm_tool.item_amount, amount)
                self.assertEqual(self.bill_farm_tool.remaining_uses, uses)

    def test_rejects_stale_inventory_version_before_start(self):
        self._assert_start_rejected(ResultCode.P3_ASSET_VERSION_CONFLICT, expected_asset_version=999)

    def test_ready_crop_starts_without_consuming_tools_or_harvesting(self):
        command = self._accept_harvest(expected_asset_version=self.bill_inv.asset_version)
        ok, code, _ = CommandService.start_command(command.command_id)
        self.assertTrue(ok, code)
        command.refresh_from_db()
        self.bill_rt.refresh_from_db()
        self.plot.refresh_from_db()
        self.bill_farm_tool.refresh_from_db()
        self.bill_inv.refresh_from_db()
        self.assertEqual(command.status, "RUNNING")
        self.assertEqual(command.started_world_minute, self.time.world_minute)
        self.assertEqual(self.bill_rt.action_status, "ACTING")
        self.assertEqual(self.bill_rt.action_ends_world_minute, self.time.world_minute + command.duration_minutes)
        self.assertEqual(self.plot.status, "GROWING")
        self.assertEqual(self.bill_farm_tool.remaining_uses, 20)
        self.assertEqual(self.bill_inv.asset_version, 0)
        self.assertFalse(self.bill_inv.slots.filter(item_id=1030).exists())

    def test_settlement_rechecks_tools_after_successful_start(self):
        command = self._accept_harvest()
        self.assertTrue(CommandService.start_command(command.command_id)[0])
        self.bill_farm_tool.item_amount = 0
        self.bill_farm_tool.save()
        self.time.world_minute += command.duration_minutes
        self.time.save()
        payload = _payload("HARVEST", 10011, "BILL_FARM", {"plot_code": self.plot.plot_code})
        payload.update(command_id=str(command.command_id), request_id=str(command.request_id),
                       message_id=str(command.message_id))
        result = commit_economy_action(payload)
        self.assertEqual(result["result_code"], ResultCode.P3_TOOL_BROKEN)
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, "GROWING")
        self.assertFalse(self.bill_inv.slots.filter(item_id=1030).exists())


# ============================================================================
# HUNT tests
# ============================================================================


class HuntSuccessTest(ProductionTestBase):
    def test_legacy_timer_hunt_is_retired(self):
        resp = self._hunt()
        self.assertEqual(resp["result_code"], "HUNT_KILL_REQUIRED")
        self.assertFalse(self.jack_inv.slots.filter(item_id__in=HUNT_MEAT_ITEM_IDS).exists())
        self.jack_hunt_tool.refresh_from_db()
        self.assertEqual(self.jack_hunt_tool.remaining_uses, 15)


class HuntFailureTest(ProductionTestBase):
    def test_hunt_wrong_npc(self):
        resp = commit_economy_action(_payload(
            "HUNT", 10011, "HUNTING_GATE",
            {"hunt_code": "HUNT_CHICKEN", "expected_asset_version": 0},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_FORBIDDEN_ACTOR)

    def test_hunt_without_formal_command_rejected_even_with_reported_location(self):
        resp = commit_economy_action(_payload(
            "HUNT", 10012, "VILLAGE_SQUARE",
            {"hunt_code": "HUNT_CHICKEN", "expected_asset_version": 0},
        ))
        self.assertEqual(resp["result_code"], "PRODUCTION_COMMAND_REQUIRED")

    def test_hunt_health_too_low(self):
        self.jack_rt.health_fp = 200
        self.jack_rt.save(update_fields=["health_fp"])
        resp = self._hunt()
        self.assertEqual(resp["result_code"], "HUNT_KILL_REQUIRED")

    def test_hunt_tool_broken(self):
        self.jack_hunt_tool.remaining_uses = 0
        self.jack_hunt_tool.save(update_fields=["remaining_uses"])
        resp = self._hunt()
        self.assertEqual(resp["result_code"], "HUNT_KILL_REQUIRED")


# ============================================================================
# COOK tests
# ============================================================================


class CookSuccessTest(ProductionTestBase):
    def setUp(self):
        super().setUp()
        # Create recipe 2 (辣子鸡丁) if not exists
        self.recipe2, _ = Recipe.objects.update_or_create(
            pk=2, defaults={"item_id": 1053, "recipe_name": "辣子鸡丁"},
        )
        RecipeIngredient.objects.get_or_create(
            recipe=self.recipe2, item_id=1056,
            defaults={"item_name": "鸡肉", "count": 2},
        )

        # Give Lucy ingredients
        self.lucy_chicken = NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1056, item_amount=5,
            produced_world_minute=600,
        )

    def test_cook_success(self):
        resp = self._cook(recipe_id=2)
        self.assertEqual(resp["result_code"], ResultCode.OK)

        # Ingredients consumed
        self.lucy_chicken.refresh_from_db()
        self.assertEqual(self.lucy_chicken.item_amount, 3)

        # Dish produced
        dish_slots = NpcGlobalInventorySlot.objects.filter(
            bag=self.lucy_inv, item_id=1053,
        )
        self.assertTrue(dish_slots.exists())
        dish = dish_slots.first()
        self.assertIn(dish.dish_quality, range(2, 7))
        self.assertEqual(dish.dish_metadata["base_quality"], 3)
        self.assertFalse(dish.rated)
        self.assertEqual(dish.expires_world_minute, 700 + 2880)


class CookFailureTest(ProductionTestBase):
    def setUp(self):
        super().setUp()
        self.recipe2, _ = Recipe.objects.update_or_create(
            pk=2, defaults={"item_id": 1053, "recipe_name": "辣子鸡丁"},
        )
        RecipeIngredient.objects.get_or_create(
            recipe=self.recipe2, item_id=1056,
            defaults={"item_name": "鸡肉", "count": 2},
        )

    def test_cook_wrong_npc(self):
        resp = commit_economy_action(_payload(
            "COOK", 10011, "RESTAURANT",
            {"recipe_id": 2, "expected_asset_version": 0},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_FORBIDDEN_ACTOR)

    def test_cook_location_is_deferred_to_unity(self):
        resp = commit_economy_action(_payload(
            "COOK", 10014, "VILLAGE_SQUARE",
            {"recipe_id": 2, "expected_asset_version": 0},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)

    def test_cook_recipe_not_allowed(self):
        resp = commit_economy_action(_payload(
            "COOK", 10014, "RESTAURANT",
            {"recipe_id": 999, "expected_asset_version": 0},
        ))
        self.assertEqual(resp["result_code"], ResultCode.P3_RECIPE_NOT_ALLOWED)

    def test_cook_insufficient_ingredients(self):
        # No ingredients in Lucy's inventory
        resp = self._cook(recipe_id=2)
        self.assertEqual(resp["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)


# ============================================================================
# Idempotent replay
# ============================================================================


class ReplayTest(ProductionTestBase):
    def test_plant_replay_returns_same_result(self):
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=2, item_id=1051, item_amount=5,
        )
        NpcGlobalInventorySlot.objects.create(
            bag=self.bill_inv, slot_index=3, item_id=4001, item_amount=10,
            remaining_uses=20,
        )
        VillageFarmPlot.objects.get_or_create(
            plot_code="FARM_PLOT_B", defaults={"status": "EMPTY"},
        )

        # Build payload ONCE so command_id/message_id are identical for replay
        payload = _payload(
            "PLANT", 10011, "BILL_FARM",
            {"plot_code": "FARM_PLOT_B", "crop_code": "CORN",
             "expected_asset_version": self.bill_inv.asset_version},
        )
        resp1 = commit_economy_action(payload)
        self.assertEqual(resp1["result_code"], ResultCode.OK)
        self.assertFalse(resp1["replayed"])

        resp2 = commit_economy_action(payload)
        self.assertEqual(resp2["result_code"], ResultCode.OK)
        self.assertTrue(resp2["replayed"])


# ============================================================================
# Concurrent operations
# ============================================================================


class ConcurrentOpsTest(ProductionTestBase):
    def test_two_harvests_on_same_plot_second_fails(self):
        """First HARVEST succeeds; second sees EMPTY plot and fails."""
        self._plant()
        self.time.world_minute = 700 + CORN_GROWTH_MINUTES
        self.time.save(update_fields=["world_minute"])
        self.bill_inv.refresh_from_db()

        # First harvest
        resp1 = self._harvest()
        self.assertEqual(resp1["result_code"], ResultCode.OK)

        # Refresh asset version after first harvest changed it
        self.bill_inv.refresh_from_db()

        # Second harvest (plot is now EMPTY, should fail with PLOT_NOT_READY)
        resp2 = commit_economy_action(_payload(
            "HARVEST", 10011, "BILL_FARM",
            {"plot_code": "FARM_PLOT_A",
             "expected_asset_version": self.bill_inv.asset_version},
        ))
        self.assertEqual(resp2["result_code"], ResultCode.P3_PLOT_NOT_READY)

    def test_cook_consumption_leaves_no_negative_inventory(self):
        """Cooking with exactly enough ingredients should succeed once, fail twice."""
        recipe2, _ = Recipe.objects.update_or_create(
            pk=2, defaults={"item_id": 1053, "recipe_name": "辣子鸡丁"},
        )
        RecipeIngredient.objects.get_or_create(
            recipe=recipe2, item_id=1056,
            defaults={"item_name": "鸡肉", "count": 2},
        )
        # Exactly 2 chicken
        NpcGlobalInventorySlot.objects.create(
            bag=self.lucy_inv, slot_index=0, item_id=1056, item_amount=2,
        )

        resp1 = self._cook(recipe_id=2)
        self.assertEqual(resp1["result_code"], ResultCode.OK)

        # Second cook should fail (no chicken left)
        self.lucy_inv.refresh_from_db()
        resp2 = commit_economy_action(_payload(
            "COOK", 10014, "RESTAURANT",
            {"recipe_id": 2,
             "expected_asset_version": self.lucy_inv.asset_version},
        ))
        self.assertEqual(resp2["result_code"], ResultCode.P3_INSUFFICIENT_ITEM)

        # No negative inventory
        chicken = NpcGlobalInventorySlot.objects.filter(
            bag=self.lucy_inv, item_id=1056,
        )
        for slot in chicken:
            self.assertGreaterEqual(slot.item_amount, 0)
