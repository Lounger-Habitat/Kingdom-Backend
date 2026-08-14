"""Phase 3 production logic: PLANT, HARVEST, HUNT, COOK.

Each handler is called inside the asset service's transaction.atomic block.
Handlers acquire their own locks and perform all mutations directly.

Lock order: farm plot (select_for_update) -> inventory (select_for_update) ->
            slot (select_for_update) via consume_fefo/consume_tool_use.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

from django.db import transaction

from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from recipes.models import Recipe, RecipeIngredient

from .models import VillageFarmPlot
from .phase3_asset_service import (
    _fail,
    _ok,
    add_batch,
    consume_fefo,
    consume_tool_use,
    deterministic_rng,
)
from .phase3_catalog import (
    CORN_BONUS_SEED_CHANCE,
    CORN_GROWTH_MINUTES,
    CORN_GUARANTEED_SEED_RETURN,
    CORN_OUTPUT_ITEM_ID,
    CORN_SEED_ITEM_ID,
    CORN_YIELD_MAX,
    CORN_YIELD_MIN,
    DISH_SPOILAGE_MINUTES,
    FARM_TOOL_ITEM_ID,
    HUNT_CHICKEN_OUTPUT_ITEM_ID,
    HUNT_CHICKEN_SPOILAGE_MINUTES,
    HUNT_CHICKEN_YIELD_MAX,
    HUNT_CHICKEN_YIELD_MIN,
    HUNT_TOOL_ITEM_ID,
    RECIPE_WHITELIST,
)
from .phase3_contract import (
    PLOT_CODES,
    ResultCode,
)


# ── PLANT ────────────────────────────────────────────────────────────────────


def _do_plant(
    npc_id: int, params: Dict[str, Any], world_minute: int, request_id: uuid.UUID
) -> Dict[str, Any]:
    plot_code = params.get("plot_code", "")
    crop_code = params.get("crop_code", "")
    expected_version = params.get("expected_asset_version")

    if plot_code not in PLOT_CODES:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if crop_code != "CORN":
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        # Lock plot first (stable order: plot_code is deterministic)
        try:
            plot = VillageFarmPlot.objects.select_for_update().get(plot_code=plot_code)
        except VillageFarmPlot.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Lock inventory
        try:
            inventory = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        if expected_version is not None and inventory.asset_version != expected_version:
            return _fail(ResultCode.P3_ASSET_VERSION_CONFLICT)

        if plot.status != "EMPTY":
            return _fail(ResultCode.P3_PLOT_NOT_READY)

        # Consume 1 corn seed (FEFO)
        try:
            consume_fefo(npc_id, (CORN_SEED_ITEM_ID,), 1)
        except ValueError:
            return _fail(ResultCode.P3_INSUFFICIENT_ITEM)

        # Consume 1 farm tool use
        try:
            consume_tool_use(npc_id, FARM_TOOL_ITEM_ID)
        except ValueError:
            return _fail(ResultCode.P3_TOOL_BROKEN)

        # Create farm plot batch with deterministic yield
        day_index = world_minute // 1440
        rng = deterministic_rng(request_id, "PLANT", day_index)
        yield_seed = rng.randint(CORN_YIELD_MIN, CORN_YIELD_MAX)
        batch_id = uuid.uuid4()

        plot.batch_id = batch_id
        plot.crop_code = "CORN"
        plot.planted_world_minute = world_minute
        plot.mature_world_minute = world_minute + CORN_GROWTH_MINUTES
        plot.yield_seed = yield_seed
        plot.source_request_id = request_id
        plot.status = "GROWING"
        plot.save()

        # Increment asset version
        inventory.asset_version += 1
        inventory.save(update_fields=["asset_version", "updated_at"])

    return _ok(
        asset_versions={str(npc_id): inventory.asset_version},
        effects={
            "items_consumed": [{"item_id": CORN_SEED_ITEM_ID, "amount": 1}],
            "tool_used": {"item_id": FARM_TOOL_ITEM_ID, "uses": 1},
            "plot": {
                "plot_code": plot_code,
                "status": "GROWING",
                "batch_id": str(batch_id),
                "mature_world_minute": world_minute + CORN_GROWTH_MINUTES,
            },
        },
    )


# ── HARVEST ──────────────────────────────────────────────────────────────────


def _do_harvest(
    npc_id: int, params: Dict[str, Any], world_minute: int, request_id: uuid.UUID
) -> Dict[str, Any]:
    plot_code = params.get("plot_code", "")
    expected_version = params.get("expected_asset_version")

    if plot_code not in PLOT_CODES:
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        try:
            plot = VillageFarmPlot.objects.select_for_update().get(plot_code=plot_code)
        except VillageFarmPlot.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        try:
            inventory = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        if expected_version is not None and inventory.asset_version != expected_version:
            return _fail(ResultCode.P3_ASSET_VERSION_CONFLICT)

        if plot.status != "GROWING":
            return _fail(ResultCode.P3_PLOT_NOT_READY)

        if plot.source_request_id == request_id:
            # Already harvested by this request_id (replay)
            return _fail(ResultCode.P3_PLOT_NOT_READY)

        if world_minute < (plot.mature_world_minute or 0):
            return _fail(ResultCode.P3_PLOT_NOT_READY)

        # Consume 1 farm tool use
        try:
            consume_tool_use(npc_id, FARM_TOOL_ITEM_ID)
        except ValueError:
            return _fail(ResultCode.P3_TOOL_BROKEN)

        # Deterministic yield from the plot's yield_seed
        day_index = world_minute // 1440
        rng = deterministic_rng(request_id, "HARVEST", day_index)
        corn_amount = plot.yield_seed or rng.randint(CORN_YIELD_MIN, CORN_YIELD_MAX)

        # Bonus seeds: 25% chance for 1 extra
        seed_amount = CORN_GUARANTEED_SEED_RETURN
        if rng.random() < CORN_BONUS_SEED_CHANCE:
            seed_amount += 1

        # Add corn batches
        added_items = []
        add_batch(
            inventory, CORN_OUTPUT_ITEM_ID, corn_amount,
            world_minute, None, request_id,
        )
        added_items.append({"item_id": CORN_OUTPUT_ITEM_ID, "amount": corn_amount})

        # Add seed returns
        add_batch(
            inventory, CORN_SEED_ITEM_ID, seed_amount,
            world_minute, None, request_id,
        )
        added_items.append({"item_id": CORN_SEED_ITEM_ID, "amount": seed_amount})

        # Clear plot
        plot.batch_id = None
        plot.crop_code = ""
        plot.planted_world_minute = None
        plot.mature_world_minute = None
        plot.yield_seed = None
        plot.source_request_id = None
        plot.status = "EMPTY"
        plot.save()

        # Increment asset version
        inventory.asset_version += 1
        inventory.save(update_fields=["asset_version", "updated_at"])

    return _ok(
        asset_versions={str(npc_id): inventory.asset_version},
        effects={
            "items_added": added_items,
            "tool_used": {"item_id": FARM_TOOL_ITEM_ID, "uses": 1},
            "plot": {"plot_code": plot_code, "status": "EMPTY"},
        },
    )


# ── HUNT ─────────────────────────────────────────────────────────────────────


def _do_hunt(
    npc_id: int, params: Dict[str, Any], world_minute: int, request_id: uuid.UUID
) -> Dict[str, Any]:
    hunt_code = params.get("hunt_code", "")
    expected_version = params.get("expected_asset_version")

    if hunt_code != "HUNT_CHICKEN":
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        try:
            inventory = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        if expected_version is not None and inventory.asset_version != expected_version:
            return _fail(ResultCode.P3_ASSET_VERSION_CONFLICT)

        # Health gate (use runtime state)
        from .models import NpcRuntimeState

        try:
            runtime = NpcRuntimeState.objects.get(npc__agent_id=str(npc_id))
        except NpcRuntimeState.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Minimum health threshold: health_fp <= 300 means too unhealthy
        if runtime.health_fp <= 300:
            return _fail(ResultCode.P3_FORBIDDEN_ACTOR)

        # Consume 1 hunt tool use
        try:
            consume_tool_use(npc_id, HUNT_TOOL_ITEM_ID)
        except ValueError:
            return _fail(ResultCode.P3_TOOL_BROKEN)

        # Deterministic yield
        day_index = world_minute // 1440
        rng = deterministic_rng(request_id, "HUNT", day_index)
        chicken_amount = rng.randint(HUNT_CHICKEN_YIELD_MIN, HUNT_CHICKEN_YIELD_MAX)

        # Add chicken with expiry
        add_batch(
            inventory,
            HUNT_CHICKEN_OUTPUT_ITEM_ID,
            chicken_amount,
            world_minute,
            world_minute + HUNT_CHICKEN_SPOILAGE_MINUTES,
            request_id,
        )

        # Increment asset version
        inventory.asset_version += 1
        inventory.save(update_fields=["asset_version", "updated_at"])

    return _ok(
        asset_versions={str(npc_id): inventory.asset_version},
        effects={
            "items_added": [
                {
                    "item_id": HUNT_CHICKEN_OUTPUT_ITEM_ID,
                    "amount": chicken_amount,
                    "expires_world_minute": world_minute + HUNT_CHICKEN_SPOILAGE_MINUTES,
                }
            ],
            "tool_used": {"item_id": HUNT_TOOL_ITEM_ID, "uses": 1},
        },
    )


# ── COOK ─────────────────────────────────────────────────────────────────────


def _do_cook(
    npc_id: int, params: Dict[str, Any], world_minute: int, request_id: uuid.UUID
) -> Dict[str, Any]:
    recipe_id = params.get("recipe_id")
    expected_version = params.get("expected_asset_version")

    if recipe_id not in RECIPE_WHITELIST:
        return _fail(ResultCode.P3_RECIPE_NOT_ALLOWED)

    with transaction.atomic():
        try:
            inventory = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        if expected_version is not None and inventory.asset_version != expected_version:
            return _fail(ResultCode.P3_ASSET_VERSION_CONFLICT)

        # The wire contract uses the authored Recipe primary key (2 / 12).
        # Recipe.item_id is the produced dish item (1053 / 1067), not recipe_id.
        try:
            recipe = Recipe.objects.get(pk=recipe_id)
        except Recipe.DoesNotExist:
            return _fail(ResultCode.P3_RECIPE_NOT_ALLOWED)

        ingredients = list(RecipeIngredient.objects.filter(recipe=recipe))
        if not ingredients:
            return _fail(ResultCode.P3_RECIPE_NOT_ALLOWED)

        # Verify sufficient ingredients
        for ing in ingredients:
            available_slots = NpcGlobalInventorySlot.objects.filter(
                bag=inventory, item_id=ing.item_id, item_amount__gt=0
            )
            total_available = sum(s.item_amount for s in available_slots)
            if total_available < ing.count:
                return _fail(ResultCode.P3_INSUFFICIENT_ITEM)

        # Consume ingredients (FEFO)
        consumed_items = []
        for ing in ingredients:
            try:
                consume_fefo(npc_id, (ing.item_id,), ing.count)
            except ValueError:
                return _fail(ResultCode.P3_INSUFFICIENT_ITEM)
            consumed_items.append({"item_id": ing.item_id, "amount": ing.count})

        # Output dish
        output_item_id = recipe.item_id
        add_batch(
            inventory,
            output_item_id,
            1,
            world_minute,
            world_minute + DISH_SPOILAGE_MINUTES,
            request_id,
        )

        # Set dish properties on the newly created slot
        output_slot = (
            NpcGlobalInventorySlot.objects.filter(bag=inventory)
            .order_by("-slot_index")
            .first()
        )
        if output_slot:
            output_slot.dish_quality = 3
            output_slot.rated = False
            output_slot.save(update_fields=["dish_quality", "rated"])

        # Increment asset version
        inventory.asset_version += 1
        inventory.save(update_fields=["asset_version", "updated_at"])

    return _ok(
        asset_versions={str(npc_id): inventory.asset_version},
        effects={
            "items_consumed": consumed_items,
            "items_added": [
                {
                    "item_id": output_item_id,
                    "amount": 1,
                    "dish_quality": 3,
                    "rated": False,
                    "expires_world_minute": world_minute + DISH_SPOILAGE_MINUTES,
                }
            ],
        },
    )
