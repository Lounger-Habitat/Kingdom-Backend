"""Phase 3 public economy actions: Nora, Lucy, Mark, EAT, COMMUNITY_MEAL.

Handlers for IMPORT_GOODS, EXPORT_GOODS, RATE_DISH, EAT, and COMMUNITY_MEAL.
Legacy public-finance handlers reject retired actions.

Each handler follows the signature:
    (npc_id, params, world_minute, request_id) -> dict

Called by ``commit_economy_action`` after contract validation.
All mutations happen inside the outer ``transaction.atomic`` block.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from django.db import models, transaction

from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType

from .models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    VillageAccount,
    VillageWorldEvent,
)
from .phase3_asset_service import (
    _fail,
    _ok,
    consume_fefo,
    get_available_slot_amount,
)
from .phase3_catalog import (
    BASE_PRICES,
    COMMUNITY_MEAL_PAYMENT_PER_DISH,
    RECIPE_WHITELIST,
)
from .phase3_contract import (
    DaySnapshotStatus,
    DeliveryStatus,
    ResultCode,
)
from .public_finance import mark_unavailable, public_fund_delta, reconcile


# ── Food whitelist for EAT ───────────────────────────────────────────────────


def _get_eat_whitelist() -> tuple:
    """Return item_ids that NPCs can eat.

    Includes configured food, recipe output items, and raw edible items.
    """
    from recipes.models import Recipe

    configured_food = Item.objects.filter(
        item_type=ItemType.FOOD, is_deleted=False,
    ).values_list("item_id", flat=True)
    recipe_outputs = list(
        Recipe.objects.filter(pk__in=RECIPE_WHITELIST).values_list("item_id", flat=True)
    )
    raw_edible = [1030, 1056]  # corn, chicken
    all_food = set(configured_food) | set(recipe_outputs) | set(raw_edible)
    return tuple(all_food)


def _get_community_meal_whitelist() -> tuple:
    """Return item_ids for community meal (Lucy's cooked dishes only)."""
    from recipes.models import Recipe

    return tuple(
        Recipe.objects.filter(pk__in=RECIPE_WHITELIST).values_list("item_id", flat=True)
    )


def _get_published_price(item_id: int) -> int:
    """Read the most recent PUBLISHED price for item_id.

    Falls back to BASE_PRICES if no snapshot exists.
    """
    snapshot = (
        DailyPriceSnapshot.objects.filter(status=DaySnapshotStatus.PUBLISHED)
        .order_by("-day_index")
        .first()
    )
    if snapshot is None:
        return BASE_PRICES.get(item_id, 10)
    prices = snapshot.prices or {}
    return prices.get(str(item_id), BASE_PRICES.get(item_id, 10))


# ── IMPORT_GOODS ─────────────────────────────────────────────────────────────


from .caravan import handle_import as handle_import_goods, handle_export as handle_export_goods


def handle_rate_dish(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    from .lucy_cooking import rate_dish
    return rate_dish(npc_id, params, world_minute, request_id)



# ── PUBLISH_PRICE_INDEX ──────────────────────────────────────────────────────


def handle_publish_price_index(npc_id, params, world_minute, request_id):
    return _fail("ACTION_RETIRED")


# ── AUDIT_PUBLIC_FUND ────────────────────────────────────────────────────────


def handle_audit_public_fund(npc_id, params, world_minute, request_id):
    return _fail("ACTION_RETIRED")


def _public_fund_money_delta(entry) -> int:
    return public_fund_delta(entry)


def _recompute_fund_balance_from_ledger() -> int:
    account = VillageAccount.objects.get(account_code="PUBLIC_FUND")
    return reconcile(account)["expected_balance"]


# ── EAT (economy enabled) ────────────────────────────────────────────────────


def handle_eat(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """EAT when economy is enabled.

    - source=PERSONAL_INVENTORY: consume the requested food instance
    - source=COMMUNITY_MEAL: FEFO consume 1 Lucy dish, PUBLIC_FUND pays Lucy
    - Return needs_delta for Unity to apply
    """
    source = params.get("source", "PERSONAL_INVENTORY")

    if source not in ("PERSONAL_INVENTORY", "COMMUNITY_MEAL"):
        return _fail(ResultCode.P3_INVALID_PARAMS)

    if source == "PERSONAL_INVENTORY":
        try:
            food_instance_id = uuid.UUID(str(params["food_instance_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            return _fail(ResultCode.P3_INVALID_PARAMS)
        return _eat_personal(npc_id, food_instance_id, world_minute, request_id)
    else:
        return _eat_community(npc_id, world_minute, request_id)


def _eat_personal(
    npc_id: int,
    food_instance_id: uuid.UUID,
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """Consume the requested food instance from the NPC's inventory."""
    try:
        inventory = NpcGlobalInventory.objects.select_for_update().get(
            npc__agent_id=str(npc_id)
        )
        slot = NpcGlobalInventorySlot.objects.select_for_update().get(
            bag=inventory,
            instance_id=food_instance_id,
        )
    except (NpcGlobalInventory.DoesNotExist, NpcGlobalInventorySlot.DoesNotExist):
        return _fail(ResultCode.P3_INSUFFICIENT_ITEM, data={
            "failure_kind": "PERSONAL_FOOD_INSTANCE_NOT_FOUND",
            "food_instance_id": str(food_instance_id),
            "message": "个人背包中没有这份食物；商店批次编号不是购买后个人食物的编号。",
        })

    if slot.item_id not in _get_eat_whitelist():
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if get_available_slot_amount(slot, world_minute) < 1:
        return _fail(ResultCode.P3_INSUFFICIENT_ITEM)

    slot.item_amount -= 1
    slot.save(update_fields=["item_amount"])
    inventory.asset_version += 1
    inventory.save(update_fields=["asset_version", "updated_at"])

    from .lucy_catalog import food_recovery
    recovery = food_recovery(slot.item_id)
    return _ok(
        asset_versions={str(npc_id): inventory.asset_version},
        effects={
            "eat": {
                "source": "PERSONAL_INVENTORY",
                "food_instance_id": str(food_instance_id),
                "items_consumed": [{"item_id": slot.item_id, "amount": 1}],
            },
        },
        external_delta=0,
        # Mirrors the authoritative needs mutation committed by the asset service.
        needs_delta=recovery,
    )


def _eat_community(
    npc_id: int,
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    return _fail("PUBLIC_MEAL_USE_STORE", data={"next_action": "BUY_FROM_STORE领取饭店0元公共餐，再EAT个人库存。"})



def _get_published_price_for_first_dish(dish_ids: tuple) -> int:
    """Get published price for the first available dish."""
    for dish_id in dish_ids:
        price = _get_published_price(dish_id)
        if price:
            return price
    return 10  # fallback


# ── COMMUNITY_MEAL ───────────────────────────────────────────────────────────


def handle_community_meal(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """Community meal: consume Lucy's dish, PUBLIC_FUND pays.

    Same as EAT with source=COMMUNITY_MEAL.
    """
    return _eat_community(npc_id, world_minute, request_id)
