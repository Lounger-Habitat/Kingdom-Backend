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
    get_available_slot_amount,
    get_usable_tool_slot,
    deterministic_rng,
)
from .phase3_catalog import (
    DISH_SPOILAGE_MINUTES,
    FARM_CROP_RULES,
    FARM_TOOL_ITEM_ID,
    HUNT_MEAT_ITEM_IDS,
    HUNT_MEAT_SPOILAGE_MINUTES,
    HUNT_MEAT_YIELD_MAX,
    HUNT_MEAT_YIELD_MIN,
    HUNT_TOOL_ITEM_ID,
    RECIPE_WHITELIST,
)
from .phase3_contract import (
    PLOT_CODES,
    ResultCode,
)


# ── PLANT ────────────────────────────────────────────────────────────────────


def plant_preflight(npc_id, params, world_minute):
    """Read-only start check; settlement repeats it under the same locks."""
    plot_code = params.get("plot_code", "")
    crop_code = params.get("crop_code", "")
    expected_version = params.get("expected_asset_version")

    if plot_code not in PLOT_CODES:
        return _fail(
            ResultCode.P3_INVALID_PARAMS,
            data={"invalid_field": "plot_code", "received_value": plot_code,
                  "allowed_values": sorted(PLOT_CODES)},
        )
    crop_rule = FARM_CROP_RULES.get(crop_code)
    if crop_rule is None:
        return _fail(
            ResultCode.P3_INVALID_PARAMS,
            data={"invalid_field": "crop_code", "received_value": crop_code,
                  "allowed_values": sorted(FARM_CROP_RULES)},
        )

    # Lock plot first (stable order: plot_code is deterministic)
    try:
        plot = VillageFarmPlot.objects.select_for_update().get(plot_code=plot_code)
    except VillageFarmPlot.DoesNotExist:
        return _fail(
            ResultCode.P3_INVALID_PARAMS,
            data={"invalid_field": "plot_code", "received_value": plot_code,
                  "failure_kind": "PLOT_NOT_FOUND"},
        )

    # Lock inventory
    try:
        inventory = NpcGlobalInventory.objects.select_for_update().get(
            npc__agent_id=str(npc_id)
        )
    except NpcGlobalInventory.DoesNotExist:
        return _fail(ResultCode.P3_INVALID_PARAMS, data={"failure_kind": "INVENTORY_NOT_FOUND"})

    if expected_version is not None and inventory.asset_version != expected_version:
        return _fail(ResultCode.P3_ASSET_VERSION_CONFLICT)

    if plot.status != "EMPTY":
        return _fail(
            ResultCode.P3_PLOT_NOT_READY,
            data={
                "failure_kind": "PLOT_ALREADY_PLANTED",
                "plot_code": plot.plot_code,
                "plot_status": plot.status,
                "crop_code": plot.crop_code or None,
                "planted_world_minute": plot.planted_world_minute,
                "mature_world_minute": plot.mature_world_minute,
            },
        )

    try:
        get_usable_tool_slot(inventory, FARM_TOOL_ITEM_ID, world_minute)
    except ValueError as exc:
        return _fail(ResultCode.P3_TOOL_BROKEN, data={"failure_kind": str(exc), "required_item_id": FARM_TOOL_ITEM_ID})

    seed_item_id = crop_rule["seed_item_id"]
    available = sum(
        get_available_slot_amount(slot, world_minute)
        for slot in inventory.slots.filter(item_id=seed_item_id, item_amount__gt=0)
    )
    if available < 1:
        return _fail(
            ResultCode.P3_INSUFFICIENT_ITEM,
            data={
                "failure_kind": (
                    "MISSING_CORN_SEED" if crop_code == "CORN" else "MISSING_CROP_SEED"
                ),
                "crop_code": crop_code,
                "required_item_id": seed_item_id,
                "required_amount": 1,
            },
        )
    return None


def _do_plant(
    npc_id: int, params: Dict[str, Any], world_minute: int, request_id: uuid.UUID
) -> Dict[str, Any]:
    failure = plant_preflight(npc_id, params, world_minute)
    if failure:
        return failure
    crop_code = params["crop_code"]
    crop_rule = FARM_CROP_RULES[crop_code]
    seed_item_id = crop_rule["seed_item_id"]
    with transaction.atomic():
        plot = VillageFarmPlot.objects.select_for_update().get(plot_code=params["plot_code"])
        inventory = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=str(npc_id))
        # Consume one seed for the selected crop (FEFO).
        try:
            consume_fefo(npc_id, (seed_item_id,), 1, world_minute)
        except ValueError:
            return _fail(
                ResultCode.P3_INSUFFICIENT_ITEM,
                data={
                    "failure_kind": (
                        "MISSING_CORN_SEED" if crop_code == "CORN" else "MISSING_CROP_SEED"
                    ),
                    "crop_code": crop_code,
                    "required_item_id": seed_item_id,
                    "required_amount": 1,
                },
            )

        # Consume 1 farm tool use
        try:
            used_tool = consume_tool_use(npc_id, FARM_TOOL_ITEM_ID, world_minute)
        except ValueError:
            return _fail(
                ResultCode.P3_TOOL_BROKEN,
                data={"failure_kind": "FARM_TOOL_UNAVAILABLE", "required_item_id": FARM_TOOL_ITEM_ID},
            )

        # Create farm plot batch with deterministic yield
        day_index = world_minute // 1440
        rng = deterministic_rng(request_id, "PLANT", day_index)
        yield_seed = rng.randint(crop_rule["yield_min"], crop_rule["yield_max"])
        batch_id = uuid.uuid4()

        plot.batch_id = batch_id
        plot.crop_code = crop_code
        plot.planted_world_minute = world_minute
        plot.mature_world_minute = world_minute + crop_rule["growth_minutes"]
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
            "items_consumed": [{"item_id": seed_item_id, "amount": 1}],
            "tool_used": {"item_id": FARM_TOOL_ITEM_ID, "instance_id": str(used_tool.instance_id), "uses": 1, "remaining_uses": used_tool.remaining_uses},
            "plot": {
                "plot_code": plot.plot_code,
                "status": "GROWING",
                "crop_code": crop_code,
                "crop_name": crop_rule.get("display_name", crop_code),
                "batch_id": str(batch_id),
                "planted_world_minute": world_minute,
                "mature_world_minute": world_minute + crop_rule["growth_minutes"],
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
        return _fail(
            ResultCode.P3_INVALID_PARAMS,
            data={"invalid_field": "plot_code", "received_value": plot_code,
                  "allowed_values": sorted(PLOT_CODES)},
        )

    with transaction.atomic():
        try:
            plot = VillageFarmPlot.objects.select_for_update().get(plot_code=plot_code)
        except VillageFarmPlot.DoesNotExist:
            return _fail(
                ResultCode.P3_INVALID_PARAMS,
                data={"invalid_field": "plot_code", "received_value": plot_code,
                      "failure_kind": "PLOT_NOT_FOUND"},
            )

        try:
            inventory = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS, data={"failure_kind": "INVENTORY_NOT_FOUND"})

        if expected_version is not None and inventory.asset_version != expected_version:
            return _fail(ResultCode.P3_ASSET_VERSION_CONFLICT)

        if plot.status != "GROWING":
            return _fail(
                ResultCode.P3_PLOT_NOT_READY,
                data={
                    "failure_kind": "PLOT_EMPTY" if plot.status == "EMPTY" else "PLOT_NOT_HARVESTABLE",
                    "plot_code": plot.plot_code,
                    "plot_status": plot.status,
                    "crop_code": plot.crop_code or None,
                    "planted_world_minute": plot.planted_world_minute,
                    "mature_world_minute": plot.mature_world_minute,
                },
            )

        if plot.source_request_id == request_id:
            # Already harvested by this request_id (replay)
            return _fail(
                ResultCode.P3_PLOT_NOT_READY,
                data={"failure_kind": "PLOT_ALREADY_HARVESTED", "plot_code": plot.plot_code},
            )

        if world_minute < (plot.mature_world_minute or 0):
            return _fail(
                ResultCode.P3_PLOT_NOT_READY,
                data={
                    "failure_kind": "PLOT_NOT_MATURE",
                    "plot_code": plot.plot_code,
                    "plot_status": plot.status,
                    "crop_code": plot.crop_code or None,
                    "planted_world_minute": plot.planted_world_minute,
                    "mature_world_minute": plot.mature_world_minute,
                },
            )

        # Consume 1 farm tool use
        try:
            used_tool = consume_tool_use(npc_id, FARM_TOOL_ITEM_ID, world_minute)
        except ValueError:
            return _fail(
                ResultCode.P3_TOOL_BROKEN,
                data={"failure_kind": "FARM_TOOL_UNAVAILABLE", "required_item_id": FARM_TOOL_ITEM_ID},
            )

        crop_code = plot.crop_code
        crop_rule = FARM_CROP_RULES.get(crop_code)
        if crop_rule is None:
            return _fail(
                ResultCode.P3_PLOT_NOT_READY,
                data={
                    "failure_kind": "PLOT_CROP_NOT_CONFIGURED",
                    "plot_code": plot.plot_code,
                    "crop_code": crop_code or None,
                },
            )

        # Capture the batch before clearing the persistent plot.  The success
        # event needs this exact context to drive one harvest presentation.
        harvested_batch_id = str(plot.batch_id) if plot.batch_id else None
        harvested_planted_world_minute = plot.planted_world_minute
        harvested_mature_world_minute = plot.mature_world_minute

        # Deterministic yield from the plot's stored yield seed.
        day_index = world_minute // 1440
        rng = deterministic_rng(request_id, "HARVEST", day_index)
        output_amount = plot.yield_seed or rng.randint(
            crop_rule["yield_min"], crop_rule["yield_max"]
        )

        seed_amount = crop_rule["guaranteed_seed_return"]
        if rng.random() < crop_rule["bonus_seed_chance"]:
            seed_amount += 1

        output_item_id = crop_rule["output_item_id"]
        seed_item_id = crop_rule["seed_item_id"]

        # Add harvested crop and returned seed batches.
        added_items = []
        add_batch(
            inventory, output_item_id, output_amount,
            world_minute, None, request_id,
        )
        added_items.append({"item_id": output_item_id, "amount": output_amount})

        # Add seed returns
        add_batch(
            inventory, seed_item_id, seed_amount,
            world_minute, None, request_id,
        )
        added_items.append({"item_id": seed_item_id, "amount": seed_amount})

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
            "tool_used": {"item_id": FARM_TOOL_ITEM_ID, "instance_id": str(used_tool.instance_id), "uses": 1, "remaining_uses": used_tool.remaining_uses},
            "plot": {
                "plot_code": plot_code,
                "status": "EMPTY",
                "crop_code": "",
                "batch_id": None,
                "planted_world_minute": None,
                "mature_world_minute": None,
                "harvested_crop_code": crop_code,
                "harvested_batch_id": harvested_batch_id,
                "harvested_planted_world_minute": harvested_planted_world_minute,
                "harvested_mature_world_minute": harvested_mature_world_minute,
                "harvested_by_npc_id": str(npc_id),
                "output": {"item_id": output_item_id, "amount": output_amount},
                "seed_return": {"item_id": seed_item_id, "amount": seed_amount},
            },
        },
    )


# ── HUNT ─────────────────────────────────────────────────────────────────────


def _do_hunt(
    npc_id: int, params: Dict[str, Any], world_minute: int, request_id: uuid.UUID
) -> Dict[str, Any]:
    hunt_code = params.get("hunt_code", "")
    if hunt_code == "HUNT_ANIMALS":
        from .jack_production import finish_work
        return finish_work(npc_id, params, world_minute, request_id)
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

        # Direct settlement calls retain the original defensive health gate;
        # the formal Hub path also rejects this before the action starts.
        from .models import NpcRuntimeState
        try:
            runtime = NpcRuntimeState.objects.get(npc__agent_id=str(npc_id))
        except NpcRuntimeState.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)
        if runtime.health_fp <= 300:
            return _fail(ResultCode.P3_FORBIDDEN_ACTOR)

        # Consume 1 hunt tool use
        try:
            used_tool = consume_tool_use(npc_id, HUNT_TOOL_ITEM_ID, world_minute)
        except ValueError:
            return _fail(ResultCode.P3_TOOL_BROKEN)

        # Deterministic yield
        day_index = world_minute // 1440
        rng = deterministic_rng(request_id, "HUNT", day_index)
        meat_item_id = rng.choice(HUNT_MEAT_ITEM_IDS)
        meat_amount = rng.randint(HUNT_MEAT_YIELD_MIN, HUNT_MEAT_YIELD_MAX)

        # Add one randomly selected meat type with expiry
        add_batch(
            inventory,
            meat_item_id,
            meat_amount,
            world_minute,
            world_minute + HUNT_MEAT_SPOILAGE_MINUTES,
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
                    "item_id": meat_item_id,
                    "amount": meat_amount,
                    "expires_world_minute": world_minute + HUNT_MEAT_SPOILAGE_MINUTES,
                }
            ],
            "tool_used": {"item_id": HUNT_TOOL_ITEM_ID, "instance_id": str(used_tool.instance_id), "uses": 1, "remaining_uses": used_tool.remaining_uses},
        },
    )


# ── COOK ─────────────────────────────────────────────────────────────────────


def _do_cook(
    npc_id: int, params: Dict[str, Any], world_minute: int, request_id: uuid.UUID
) -> Dict[str, Any]:
    from .lucy_cooking import cook
    return cook(npc_id, params, world_minute, request_id)
