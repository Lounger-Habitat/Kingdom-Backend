"""Phase 3 public economy actions: Nora, Lucy, Mark, EAT, COMMUNITY_MEAL.

Handlers for IMPORT_GOODS, EXPORT_GOODS, RATE_DISH, PUBLISH_PRICE_INDEX,
AUDIT_PUBLIC_FUND, EAT, and COMMUNITY_MEAL.

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
    NpcTradeDelivery,
    VillageAccount,
    VillageWorldEvent,
)
from .phase3_asset_service import (
    _fail,
    _ok,
    add_batch,
    consume_fefo,
    get_available_slot_amount,
)
from .phase3_catalog import (
    BASE_PRICES,
    CARAVAN_COOLDOWN_DAYS,
    COMMUNITY_MEAL_PAYMENT_PER_DISH,
    IMPORT_WHITELIST,
    EXPORT_WHITELIST,
    MARK_SALARY,
    RECIPE_WHITELIST,
    SAFE_STOCKS,
    TOOL_ITEM_IDS,
)
from .phase3_contract import (
    DaySnapshotStatus,
    DeliveryStatus,
    ResultCode,
)


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


# ── Mark salary helper ───────────────────────────────────────────────────────


def _pay_mark_salary(
    account: VillageAccount,
    mark_inv: NpcGlobalInventory,
    changes: Dict[str, Any],
) -> bool:
    """Pay Mark 1 gold from PUBLIC_FUND. Skip if fund empty (not an error).

    Internal transfer: PUBLIC_FUND -> Mark's NPC inventory.
    Both account and mark_inv must already be select_for_update locked.
    Returns True if salary was paid.
    """
    if account.balance >= MARK_SALARY:
        account.balance -= MARK_SALARY
        account.save(update_fields=["balance", "updated_at"])
        mark_inv.money += MARK_SALARY
        mark_inv.save(update_fields=["money", "updated_at"])
        changes["mark_salary"] = MARK_SALARY
        return True
    else:
        changes["mark_salary"] = 0
        changes["salary_skipped"] = True
        return False


# ── IMPORT_GOODS ─────────────────────────────────────────────────────────────


def handle_import_goods(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """Nora imports items from external caravan.

    - Actor 10015, location TRADE_ROAD
    - Item must be in IMPORT_WHITELIST
    - Cooldown: last_caravan_day >= 3 days ago
    - Import amount = safe_stock - available_amount (capped at requested amount)
    - Nora's own inventory balance pays price * amount
    - external_delta = -(price * amount)
    """
    item_id = params.get("item_id")
    amount = params.get("amount")

    if not isinstance(item_id, int) or item_id not in IMPORT_WHITELIST:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    item = Item.objects.filter(item_id=item_id, is_deleted=False).first()

    with transaction.atomic():
        # Lock the shared caravan cooldown record; it does not fund the import.
        try:
            account = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
        except VillageAccount.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Lock inventory
        try:
            inventory = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Cooldown check
        last_day = account.last_caravan_day
        current_day = world_minute // 1440
        if last_day is not None and (current_day - last_day) < CARAVAN_COOLDOWN_DAYS:
            return _fail(ResultCode.P3_CARAVAN_COOLDOWN)

        # Calculate how much to import (up to safe_stock)
        safe_stock = SAFE_STOCKS.get(item_id, 0)
        available = sum(
            get_available_slot_amount(s)
            for s in NpcGlobalInventorySlot.objects.filter(
                bag=inventory, item_id=item_id, item_amount__gt=0
            )
        )
        max_import = max(0, safe_stock - available)
        actual_amount = min(amount, max_import)

        if actual_amount <= 0:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Calculate price
        unit_price = _get_published_price(item_id)
        total_cost = unit_price * actual_amount

        # Nora is the store owner, so she pays for her own stock.
        if inventory.money < total_cost:
            return _fail(ResultCode.P3_INSUFFICIENT_MONEY)

        # Execute
        account.last_caravan_day = current_day
        account.save(update_fields=["last_caravan_day", "updated_at"])
        inventory.money -= total_cost

        # Add items to Nora's inventory
        add_batch(
            inventory, item_id, actual_amount,
            world_minute, None, request_id,
            remaining_uses=(
                item.max_use_count
                if item is not None and item_id in TOOL_ITEM_IDS
                else None
            ),
        )

        inventory.asset_version += 1
        inventory.save(update_fields=["money", "asset_version", "updated_at"])

    return _ok(
        asset_versions={str(npc_id): inventory.asset_version},
        effects={
            "import": {
                "item_id": item_id,
                "amount": actual_amount,
                "unit_price": unit_price,
                "total_cost": total_cost,
            },
            "nora_balance": inventory.money,
            "public_fund_balance": account.balance,
        },
        external_delta=-total_cost,
    )


# ── EXPORT_GOODS ─────────────────────────────────────────────────────────────


def handle_export_goods(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """Nora exports surplus items.

    - Actor 10015, location TRADE_ROAD
    - Item must be in EXPORT_WHITELIST
    - Can only export available amount above safe_stock
    - Deduct inventory, PUBLIC_FUND receives price * amount
    - external_delta = +(price * amount)
    """
    item_id = params.get("item_id")
    amount = params.get("amount")
    expected_version = params.get("expected_asset_version")

    if not isinstance(item_id, int) or item_id not in EXPORT_WHITELIST:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        # Lock account
        try:
            account = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
        except VillageAccount.DoesNotExist:
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

        # Calculate surplus above safe_stock
        safe_stock = SAFE_STOCKS.get(item_id, 0)
        slots = list(
            NpcGlobalInventorySlot.objects.select_for_update()
            .filter(bag=inventory, item_id=item_id, item_amount__gt=0)
            .order_by(
                models.F("expires_world_minute").asc(nulls_last=True),
                models.F("produced_world_minute").asc(nulls_last=True),
                "instance_id",
            )
        )
        total_available = sum(get_available_slot_amount(s) for s in slots)
        surplus = max(0, total_available - safe_stock)
        actual_amount = min(amount, surplus)

        if actual_amount <= 0:
            return _fail(ResultCode.P3_INSUFFICIENT_ITEM)

        # Calculate price
        unit_price = _get_published_price(item_id)
        total_income = unit_price * actual_amount

        # Deduct items (FEFO)
        remaining = actual_amount
        for slot in slots:
            if remaining <= 0:
                break
            avail = get_available_slot_amount(slot)
            take = min(avail, remaining)
            if take > 0:
                slot.item_amount -= take
                slot.save(update_fields=["item_amount"])
                remaining -= take

        # Add money to PUBLIC_FUND
        account.balance += total_income
        account.save(update_fields=["balance", "updated_at"])

        inventory.asset_version += 1
        inventory.save(update_fields=["asset_version", "updated_at"])

    return _ok(
        asset_versions={str(npc_id): inventory.asset_version},
        effects={
            "export": {
                "item_id": item_id,
                "amount": actual_amount,
                "unit_price": unit_price,
                "total_income": total_income,
            },
            "public_fund_balance": account.balance,
        },
        external_delta=total_income,
    )


# ── RATE_DISH ────────────────────────────────────────────────────────────────


def handle_rate_dish(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """Mark rates a dish.

    - Actor 10013, location RECORD_HALL
    - Lock owner's food slot by food_instance_id
    - Set rated=true, score=1..5
    - Cannot rate already rated dish
    - Pay Mark 1 gold from PUBLIC_FUND (skip if fund empty)
    """
    owner_npc_id = params.get("owner_npc_id")
    food_instance_id = params.get("food_instance_id")
    score = params.get("score")

    if not isinstance(score, int) or isinstance(score, bool) or score < 1 or score > 5:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if owner_npc_id is None or food_instance_id is None:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    try:
        food_instance_uuid = uuid.UUID(str(food_instance_id))
    except (TypeError, ValueError, AttributeError):
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        # Lock account and mark_inv (stable order by account_code)
        try:
            account = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
        except VillageAccount.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        mark_inv = NpcGlobalInventory.objects.select_for_update().get(
            npc__agent_id="10013"
        )

        # Find the owner's inventory
        try:
            owner_inv = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(owner_npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Find the slot by instance_id
        try:
            slot = NpcGlobalInventorySlot.objects.select_for_update().get(
                bag=owner_inv,
                instance_id=food_instance_uuid,
            )
        except NpcGlobalInventorySlot.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        dish_item_ids = _get_community_meal_whitelist()
        if slot.item_amount <= 0 or slot.item_id not in dish_item_ids:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Cannot rate already rated dish
        if slot.rated:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Rate the dish
        slot.rated = True
        slot.overall_score = score
        slot.save(update_fields=["rated", "overall_score"])

        # Pay Mark salary
        changes: Dict[str, Any] = {
            "dish_rated": {
                "owner_npc_id": owner_npc_id,
                "food_instance_id": str(food_instance_id),
                "score": score,
            },
        }
        _pay_mark_salary(account, mark_inv, changes)

        owner_inv.asset_version += 1
        owner_inv.save(update_fields=["asset_version", "updated_at"])

    return _ok(
        asset_versions={str(owner_npc_id): owner_inv.asset_version},
        effects=changes,
        external_delta=0,
    )


# ── PUBLISH_PRICE_INDEX ──────────────────────────────────────────────────────


def handle_publish_price_index(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """Mark publishes daily price index.

    - Actor 10013, location RECORD_HALL
    - Change DRAFT -> PUBLISHED for specified day_index
    - Pay Mark 1 gold from PUBLIC_FUND
    """
    day_index = params.get("day_index")
    if not isinstance(day_index, int) or isinstance(day_index, bool) or day_index < 0:
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        # Lock account and mark_inv
        try:
            account = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
        except VillageAccount.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        mark_inv = NpcGlobalInventory.objects.select_for_update().get(
            npc__agent_id="10013"
        )

        # Find the DRAFT snapshot
        try:
            snapshot = DailyPriceSnapshot.objects.select_for_update().get(
                day_index=day_index,
                status=DaySnapshotStatus.DRAFT,
            )
        except DailyPriceSnapshot.DoesNotExist:
            return _fail(ResultCode.P3_PRICE_UNAVAILABLE)

        # Publish
        snapshot.status = DaySnapshotStatus.PUBLISHED
        snapshot.published_by_npc_id = npc_id
        snapshot.published_world_minute = world_minute
        snapshot.save(update_fields=[
            "status", "published_by_npc_id", "published_world_minute",
        ])

        # Pay Mark salary
        changes: Dict[str, Any] = {
            "price_published": {
                "day_index": day_index,
                "prices": snapshot.prices,
            },
        }
        _pay_mark_salary(account, mark_inv, changes)

    return _ok(
        effects=changes,
        external_delta=0,
    )


# ── AUDIT_PUBLIC_FUND ────────────────────────────────────────────────────────


def handle_audit_public_fund(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """Mark audits public fund.

    - Actor 10013, location RECORD_HALL
    - Recalculate expected balance from ledger
    - Compare with actual VillageAccount.balance
    - Write audit event
    - Pay Mark 1 gold from PUBLIC_FUND
    """
    day_index = params.get("day_index")
    if not isinstance(day_index, int) or isinstance(day_index, bool) or day_index < 0:
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        # Lock account and mark_inv
        try:
            account = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
        except VillageAccount.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        mark_inv = NpcGlobalInventory.objects.select_for_update().get(
            npc__agent_id="10013"
        )

        # Recalculate expected balance from all ledger entries
        expected_balance = _recompute_fund_balance_from_ledger()

        balance_matches = (expected_balance == account.balance)

        # Pay Mark salary
        changes: Dict[str, Any] = {
            "audit": {
                "day_index": day_index,
                "expected_balance": expected_balance,
                "actual_balance": account.balance,
                "balance_matches": balance_matches,
            },
        }
        _pay_mark_salary(account, mark_inv, changes)

    return _ok(
        effects=changes,
        external_delta=0,
    )


def _public_fund_money_delta(entry) -> int:
    """Extract the PUBLIC_FUND money delta recorded on a ledger entry.

    Store transaction fees are recorded under ``changes["PUBLIC_FUND"]["money"]``
    (positive = income).  Only EXPORT_GOODS changes PUBLIC_FUND directly;
    IMPORT_GOODS is paid by Nora and its ``external_delta`` is external only.
    """
    delta = 0
    changes = entry.changes or {}
    pf = changes.get("PUBLIC_FUND")
    if isinstance(pf, dict):
        try:
            delta += int(pf.get("money", 0) or 0)
        except (TypeError, ValueError):
            pass
    if entry.entry_type == "EXPORT_GOODS":
        try:
            delta += int(entry.external_delta or 0)
        except (TypeError, ValueError):
            pass
    return delta


def _recompute_fund_balance_from_ledger() -> int:
    """Recompute PUBLIC_FUND balance from every ``EconomicLedgerEntry``.

    There is no stored opening-balance snapshot on ``VillageAccount`` (models
    are frozen), so the opening balance is derived as
    ``current_balance - recorded_deltas`` and then the recorded PUBLIC_FUND
    deltas are summed back.  This makes the recompute ledger-driven (including
    store transaction fees) and internally consistent: if a change to
    PUBLIC_FUND is missing from the ledger, the derived opening balance shifts
    accordingly, so the audit reconciles against the ledger's own records.
    """
    net_delta = sum(
        _public_fund_money_delta(e)
        for e in EconomicLedgerEntry.objects.order_by("world_minute", "id")
    )
    try:
        current = VillageAccount.objects.get(account_code="PUBLIC_FUND").balance
    except VillageAccount.DoesNotExist:
        current = 0
    opening = current - net_delta
    return opening + net_delta


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
        return _fail(ResultCode.P3_INSUFFICIENT_ITEM)

    if slot.item_id not in _get_eat_whitelist():
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if get_available_slot_amount(slot) < 1:
        return _fail(ResultCode.P3_INSUFFICIENT_ITEM)

    slot.item_amount -= 1
    slot.save(update_fields=["item_amount"])
    inventory.asset_version += 1
    inventory.save(update_fields=["asset_version", "updated_at"])

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
        needs_delta={
            "hunger_fp": -450,
            "energy_fp": 50,
        },
    )


def _eat_community(
    npc_id: int,
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """FEFO consume 1 Lucy dish, PUBLIC_FUND pays Lucy.

    Fails if no dishes or fund insufficient.
    """
    dish_whitelist = _get_community_meal_whitelist()
    if not dish_whitelist:
        return _fail(ResultCode.P3_INSUFFICIENT_ITEM)

    with transaction.atomic():
        # Lock account
        try:
            account = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")
        except VillageAccount.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Check fund can pay
        dish_price = _get_published_price_for_first_dish(dish_whitelist)
        if account.balance < dish_price:
            return _fail(ResultCode.P3_INSUFFICIENT_MONEY)

        # Consume 1 dish from Lucy's inventory (FEFO)
        try:
            consumed = consume_fefo(10014, dish_whitelist, 1)
        except ValueError:
            return _fail(ResultCode.P3_INSUFFICIENT_ITEM)

        # Pay Lucy from PUBLIC_FUND
        lucy_inv = NpcGlobalInventory.objects.select_for_update().get(
            npc__agent_id="10014"
        )
        account.balance -= dish_price
        account.save(update_fields=["balance", "updated_at"])

        lucy_inv.money += dish_price
        lucy_inv.asset_version += 1
        lucy_inv.save(update_fields=["money", "asset_version", "updated_at"])

        # Bump eater's asset version
        eater_inv = NpcGlobalInventory.objects.select_for_update().get(
            npc__agent_id=str(npc_id)
        )
        eater_inv.asset_version += 1
        eater_inv.save(update_fields=["asset_version", "updated_at"])

    return _ok(
        asset_versions={
            str(npc_id): eater_inv.asset_version,
            "10014": lucy_inv.asset_version,
        },
        effects={
            "eat": {
                "source": "COMMUNITY_MEAL",
                "items_consumed": [
                    {"item_id": slot.item_id, "amount": amt}
                    for slot, amt in consumed
                ],
                "payment": dish_price,
                "public_fund_balance": account.balance,
            },
        },
        external_delta=0,
        needs_delta={
            "hunger_fp": -450,
            "energy_fp": 50,
        },
    )


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
