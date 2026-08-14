"""Phase 3 day-close service.

Idempotent, sequential day-close that runs:
  1. Expire OPEN/MATCHED orders and ACTIVE deliveries
  2. Spoil expired food (SPOILAGE ledger)
  3. Lucy restaurant traveler sales (max 2 per day, FEFO)
  4. Generate DailyPriceSnapshot(DRAFT) for next day
  5. Update restaurant and store public status
  6. Save day-close receipt + ledger/event summary

Called from the day-close API endpoint, not from commit_economy_action.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Tuple

from django.db import models, transaction

from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot

from .models import (
    DailyPriceSnapshot,
    EconomicLedgerEntry,
    NpcCommandState,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageActionReceipt,
    VillageRestaurantState,
    VillageSimulationConfig,
    VillageWorldEvent,
)
from .phase3_catalog import (
    BASE_PRICES,
    SAFE_STOCKS,
    TRAVELER_MAX_SALES,
)
from .phase3_contract import (
    DaySnapshotStatus,
    DeliveryStatus,
    OrderStatus,
    RestaurantStatus,
    ResultCode,
    StoreStatus,
)


# ── Price calculation ────────────────────────────────────────────────────────


def calculate_price(base_price: int, available_amount: int, safe_stock: int) -> int:
    """Calculate market price based on supply vs safe stock.

    Low stock (< safe_stock): 1.25x base price
    Normal (safe_stock .. 2*safe_stock): base price
    Surplus (> 2*safe_stock): 0.85x base price
    Minimum price is always 1.
    """
    if available_amount < safe_stock:
        return max(1, round(base_price * 1.25))
    elif available_amount <= 2 * safe_stock:
        return max(1, base_price)
    else:
        return max(1, round(base_price * 0.85))


# ── Status helpers ───────────────────────────────────────────────────────────


def _get_available_dish_count(lucy_npc_id: int = 10014) -> int:
    """Count Lucy's unexpired, unreserved cooked dishes.

    Recipe output item_ids are determined from RECIPE_WHITELIST.
    """
    from recipes.models import Recipe
    from .phase3_catalog import RECIPE_WHITELIST

    recipe_output_ids = list(
        Recipe.objects.filter(pk__in=RECIPE_WHITELIST).values_list("item_id", flat=True)
    )
    if not recipe_output_ids:
        return 0

    from .models import NpcTradeDelivery as _DTD

    try:
        inventory = NpcGlobalInventory.objects.get(npc__agent_id=str(lucy_npc_id))
    except NpcGlobalInventory.DoesNotExist:
        return 0

    slots = NpcGlobalInventorySlot.objects.filter(
        bag=inventory,
        item_id__in=recipe_output_ids,
        item_amount__gt=0,
    )
    total = 0
    for slot in slots:
        # Skip expired
        if slot.expires_world_minute is not None:
            # caller passes current world minute context; we check in the caller
            pass
        # Deduct ACTIVE delivery reservations
        reserved = (
            _DTD.objects.filter(
                seller_slot_instance_id=slot.instance_id, status="ACTIVE"
            ).aggregate(total=models.Sum("reserved_amount"))["total"]
            or 0
        )
        available = max(0, slot.item_amount - reserved)
        total += available
    return total


def get_restaurant_status(available_dish_count: int) -> str:
    """Return restaurant status based on available dishes."""
    if available_dish_count <= 0:
        return RestaurantStatus.SOLD_OUT
    if available_dish_count < TRAVELER_MAX_SALES:
        return RestaurantStatus.LOW_STOCK
    return RestaurantStatus.OPEN


def get_store_status(world_minute: int) -> str:
    """Return GENERAL_STORE status from the store's independent inventory.

    Reads ``VillageStoreInventorySlot`` (not Nora's personal bag), excluding
    expired batches and reserved quantities, and compares against SAFE_STOCKS.
    """
    from .models import VillageStore, VillageStoreInventorySlot

    try:
        store = VillageStore.objects.get(store_code="GENERAL_STORE")
    except VillageStore.DoesNotExist:
        return StoreStatus.OUT_OF_STOCK

    available_by_item: Dict[int, int] = {}
    for slot in VillageStoreInventorySlot.objects.filter(
        store=store, item_amount__gt=0
    ):
        if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
            continue
        available = max(0, slot.item_amount - slot.reserved_amount)
        available_by_item[slot.item_id] = available_by_item.get(slot.item_id, 0) + available

    has_out = False
    has_low = False
    for item_id, safe in SAFE_STOCKS.items():
        available = available_by_item.get(item_id, 0)
        if available <= 0 and safe > 0:
            has_out = True
        elif 0 < available < safe:
            has_low = True

    if has_out:
        return StoreStatus.OUT_OF_STOCK
    if has_low:
        return StoreStatus.LOW_STOCK
    return StoreStatus.NORMAL


# ── Main day-close entry point ───────────────────────────────────────────────


def handle_day_close(
    payload: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """Execute day-close for one day. Idempotent per request_id.

    Parameters
    ----------
    payload : dict
        Must contain ``day_index``.
    world_minute : int
        Current trusted world minute.
    request_id : uuid.UUID
        Deterministic UUIDv5 from ``phase3:day-close:{day_index}``.

    Returns
    -------
    dict
        Standard economy result envelope.
    """
    day_index = payload.get("day_index")
    if day_index is None or not isinstance(day_index, int) or isinstance(day_index, bool) or day_index < 0:
        return _fail(ResultCode.P3_INVALID_PARAMS)

    try:
        account = VillageAccount.objects.get(account_code="PUBLIC_FUND")
    except VillageAccount.DoesNotExist:
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        # Lock account
        account = VillageAccount.objects.select_for_update().get(account_code="PUBLIC_FUND")

        # Check idempotency
        existing = VillageActionReceipt.objects.filter(request_id=request_id).first()
        if existing:
            response_data = dict(existing.response_data or {})
            response_data["replayed"] = True
            return response_data

        # Sequential check
        expected_day = account.last_closed_day_index + 1
        if day_index != expected_day:
            return _fail(ResultCode.P3_DAY_NOT_READY)

        # Cannot close future days
        current_day = world_minute // 1440
        if day_index > current_day:
            return _fail(ResultCode.P3_DAY_NOT_READY)

        day_end_minute = (day_index + 1) * 1440 - 1

        all_changes: Dict[str, Any] = {}
        total_external_delta = 0
        ledger_summaries: List[Dict[str, Any]] = []

        # ── Step 1: Expire orders and deliveries ─────────────────────────────
        expire_result = _expire_orders_and_deliveries(day_end_minute)
        all_changes["expired"] = expire_result

        # ── Step 2: Spoil food ───────────────────────────────────────────────
        spoilage_result, spoilage_external = _spoil_food(day_end_minute)
        all_changes["spoilage"] = spoilage_result
        # spoilage_external is always 0 (items just disappear)
        ledger_summaries.append({
            "type": "SPOILAGE",
            "items_spoiled": spoilage_result.get("items_spoiled", []),
            "count": spoilage_result.get("count", 0),
        })

        # ── Step 3: Lucy restaurant traveler sales ───────────────────────────
        sales_result, sales_external = _traveler_sales(
            day_index, day_end_minute, request_id,
        )
        all_changes["traveler_sales"] = sales_result
        total_external_delta += sales_external
        if sales_result.get("sold", 0) > 0:
            ledger_summaries.append({
                "type": "TRAVELER_SALE",
                "sold": sales_result["sold"],
                "revenue": sales_result["revenue"],
            })

        # ── Step 4: Generate DailyPriceSnapshot(DRAFT) ───────────────────────
        price_snapshot = _generate_price_snapshot(day_index + 1, world_minute)
        all_changes["price_snapshot"] = price_snapshot

        # ── Step 5: Update restaurant and store status ───────────────────────
        lucy_available = _get_available_dish_count_with_expiry(day_end_minute)
        restaurant_status = get_restaurant_status(lucy_available)
        store_status = get_store_status(world_minute)

        # Create or update restaurant state
        rest_state, _ = VillageRestaurantState.objects.update_or_create(
            day_index=day_index,
            defaults={
                "status": restaurant_status,
                "traveler_sales_count": sales_result.get("sold", 0),
                "traveler_revenue": sales_result.get("revenue", 0),
            },
        )
        all_changes["restaurant_status"] = restaurant_status
        all_changes["store_status"] = store_status

        # ── Step 6: Update account and save receipt ──────────────────────────
        account.last_closed_day_index = day_index
        account.asset_version += 1
        account.save(update_fields=["last_closed_day_index", "asset_version", "updated_at"])

        # Build result
        result = _ok(
            world_minute=world_minute,
            external_delta=total_external_delta,
            data={
                "day_index": day_index,
                "day_end_minute": day_end_minute,
                "restaurant_status": restaurant_status,
                "store_status": store_status,
                "traveler_sales": sales_result,
                "spoilage": spoilage_result,
                "expired": expire_result,
                "price_snapshot_day": day_index + 1,
                "ledger_summaries": ledger_summaries,
            },
        )

        # World event
        npc_mark = _get_npc_obj(10013)
        event = VillageWorldEvent.objects.create(
            dedupe_key=f"day-close:{request_id}",
            event_type="DAY_CLOSE",
            world_minute=world_minute,
            time_revision=world_minute,
            actor_npc=npc_mark,
            participant_npc_ids=["10013"],
            location_code="RECORD_HALL",
            request_id=request_id,
            effects={"day_index": day_index},
            visibility="PUBLIC",
            public_data={"day_index": day_index, "restaurant_status": restaurant_status},
        )
        result["event"] = {
            "event_id": str(event.event_id),
            "event_sequence": event.event_sequence,
        }

        # NpcCommandState (terminal placeholder -- day-close has no single NPC actor)
        command = NpcCommandState.objects.create(
            command_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            request_id=request_id,
            npc=npc_mark,
            schema_version="village.day-close.v1",
            sender_id="day-close-service",
            recipient_id="economy-service",
            kind="DAY_CLOSE",
            location_code="RECORD_HALL",
            sanitized_parameters={"day_index": day_index},
            payload_sha256="",
            queue_mode="STRICT",
            status="SUCCEEDED",
            completed_world_minute=world_minute,
            received_world_minute=world_minute,
            result_code=ResultCode.OK,
        )

        # Ledger entry
        EconomicLedgerEntry.objects.create(
            request_id=request_id,
            entry_type="DAY_CLOSE",
            actor_npc=npc_mark,
            changes={
                "expired_orders": expire_result.get("orders", 0),
                "expired_deliveries": expire_result.get("deliveries", 0),
                "spoilage_count": spoilage_result.get("count", 0),
                "traveler_sales": sales_result.get("sold", 0),
                "traveler_revenue": sales_result.get("revenue", 0),
            },
            external_delta=total_external_delta,
            world_minute=world_minute,
            event=event,
        )

        # Receipt
        response_data = dict(result)
        VillageActionReceipt.objects.create(
            request_id=request_id,
            command=command,
            npc=npc_mark,
            action_kind="DAY_CLOSE",
            success=True,
            result_code=ResultCode.OK,
            response_data=response_data,
            world_minute=world_minute,
            time_revision=world_minute,
            world_event=event,
        )

        return result


# ── Internal steps ───────────────────────────────────────────────────────────


def _expire_orders_and_deliveries(day_end_minute: int) -> Dict[str, Any]:
    """Release expired OPEN/MATCHED orders and ACTIVE deliveries."""
    # Expire OPEN orders
    open_expired = NpcTradeOrder.objects.filter(
        status=OrderStatus.OPEN,
        expires_world_minute__lte=day_end_minute,
    ).update(status=OrderStatus.EXPIRED)

    # Expire MATCHED orders
    matched_expired = NpcTradeOrder.objects.filter(
        status=OrderStatus.MATCHED,
        expires_world_minute__lte=day_end_minute,
    ).update(status=OrderStatus.EXPIRED)

    # Expire ACTIVE deliveries (reservation release is implicit -- status change)
    delivery_expired = NpcTradeDelivery.objects.filter(
        status=DeliveryStatus.ACTIVE,
        expires_world_minute__lte=day_end_minute,
    ).update(status=DeliveryStatus.EXPIRED)

    return {
        "orders": open_expired + matched_expired,
        "deliveries": delivery_expired,
    }


def _spoil_food(day_end_minute: int) -> Tuple[Dict[str, Any], int]:
    """Delete/spoil food items where expires_world_minute <= day_end_minute
    and not reserved by an ACTIVE delivery.

    Returns (result_dict, external_delta) where external_delta is always 0.
    """
    from .phase3_asset_service import get_available_slot_amount

    # Find all food slots that expire within the day
    food_slots = NpcGlobalInventorySlot.objects.select_for_update().filter(
        expires_world_minute__isnull=False,
        expires_world_minute__lte=day_end_minute,
        item_amount__gt=0,
    )

    items_spoiled: List[Dict[str, Any]] = []
    total_spoiled = 0

    for slot in food_slots:
        # Check if reserved by ACTIVE delivery
        reserved = (
            NpcTradeDelivery.objects.filter(
                seller_slot_instance_id=slot.instance_id,
                status=DeliveryStatus.ACTIVE,
            ).aggregate(total=models.Sum("reserved_amount"))["total"]
            or 0
        )
        spoilable = max(0, slot.item_amount - reserved)
        if spoilable > 0:
            items_spoiled.append({
                "item_id": slot.item_id,
                "amount": spoilable,
                "instance_id": str(slot.instance_id),
            })
            slot.item_amount = reserved  # keep only reserved portion
            if slot.item_amount <= 0:
                slot.delete()
            else:
                slot.save(update_fields=["item_amount"])
            total_spoiled += spoilable

    return {"items_spoiled": items_spoiled, "count": total_spoiled}, 0


def _get_available_dish_count_with_expiry(day_end_minute: int) -> int:
    """Count Lucy's dishes that are unexpired (after day-close) and unreserved."""
    from recipes.models import Recipe
    from .phase3_catalog import RECIPE_WHITELIST

    recipe_output_ids = list(
        Recipe.objects.filter(pk__in=RECIPE_WHITELIST).values_list("item_id", flat=True)
    )
    if not recipe_output_ids:
        return 0

    try:
        inventory = NpcGlobalInventory.objects.get(npc__agent_id="10014")
    except NpcGlobalInventory.DoesNotExist:
        return 0

    slots = NpcGlobalInventorySlot.objects.filter(
        bag=inventory,
        item_id__in=recipe_output_ids,
        item_amount__gt=0,
    )

    total = 0
    for slot in slots:
        # Only count if NOT expired at day end
        if slot.expires_world_minute is not None and slot.expires_world_minute <= day_end_minute:
            continue
        reserved = (
            NpcTradeDelivery.objects.filter(
                seller_slot_instance_id=slot.instance_id,
                status=DeliveryStatus.ACTIVE,
            ).aggregate(total=models.Sum("reserved_amount"))["total"]
            or 0
        )
        total += max(0, slot.item_amount - reserved)
    return total


def _traveler_sales(
    day_index: int,
    day_end_minute: int,
    request_id: uuid.UUID,
) -> Tuple[Dict[str, Any], int]:
    """Lucy restaurant: sell up to TRAVELER_MAX_SALES unexpired, unreserved
    dishes to external travelers at recent published price.

    Returns (result_dict, external_delta).
    """
    from recipes.models import Recipe
    from .phase3_catalog import RECIPE_WHITELIST
    from .phase3_asset_service import deterministic_rng

    recipe_output_ids = list(
        Recipe.objects.filter(pk__in=RECIPE_WHITELIST).values_list("item_id", flat=True)
    )
    if not recipe_output_ids:
        return {"sold": 0, "revenue": 0}, 0

    try:
        lucy_inv = NpcGlobalInventory.objects.select_for_update().get(
            npc__agent_id="10014"
        )
    except NpcGlobalInventory.DoesNotExist:
        return {"sold": 0, "revenue": 0}, 0

    # Get published price for the most recent published snapshot
    published_price = _get_latest_published_price()

    # FEFO order: earliest expiry first
    slots = list(
        NpcGlobalInventorySlot.objects.select_for_update()
        .filter(
            bag=lucy_inv,
            item_id__in=recipe_output_ids,
            item_amount__gt=0,
        )
        .order_by(
            models.F("expires_world_minute").asc(nulls_last=True),
            models.F("produced_world_minute").asc(nulls_last=True),
            "instance_id",
        )
    )

    sold = 0
    revenue = 0

    for slot in slots:
        if sold >= TRAVELER_MAX_SALES:
            break

        # Skip expired dishes
        if slot.expires_world_minute is not None and slot.expires_world_minute <= day_end_minute:
            continue

        # Skip reserved items
        reserved = (
            NpcTradeDelivery.objects.filter(
                seller_slot_instance_id=slot.instance_id,
                status=DeliveryStatus.ACTIVE,
            ).aggregate(total=models.Sum("reserved_amount"))["total"]
            or 0
        )
        available = max(0, slot.item_amount - reserved)
        if available <= 0:
            continue

        # Sell 1 dish
        dish_price = published_price.get(slot.item_id, BASE_PRICES.get(slot.item_id, 10))
        slot.item_amount -= 1
        slot.save(update_fields=["item_amount"])
        lucy_inv.money += dish_price
        sold += 1
        revenue += dish_price

    if sold > 0:
        lucy_inv.save(update_fields=["money", "updated_at"])

    return {"sold": sold, "revenue": revenue}, revenue


def _get_latest_published_price() -> Dict[int, int]:
    """Get prices from the most recent PUBLISHED snapshot. Empty dict if none."""
    snapshot = (
        DailyPriceSnapshot.objects.filter(status=DaySnapshotStatus.PUBLISHED)
        .order_by("-day_index")
        .first()
    )
    if snapshot is None:
        return {}
    # prices JSON keys are strings
    return {int(k): v for k, v in (snapshot.prices or {}).items()}


def _generate_price_snapshot(
    target_day_index: int,
    world_minute: int,
) -> Dict[str, int]:
    """Generate DailyPriceSnapshot(DRAFT) for the given day_index.

    Prices based on total available inventory across all NPC inventories.
    """
    from .phase3_asset_service import get_available_slot_amount

    prices: Dict[int, int] = {}

    for item_id, base_price in BASE_PRICES.items():
        safe_stock = SAFE_STOCKS.get(item_id, 0)
        if safe_stock <= 0:
            # No safe stock defined = fixed price
            prices[item_id] = base_price
            continue

        # Sum available inventory across all NPCs
        total_available = 0
        slots = NpcGlobalInventorySlot.objects.filter(
            item_id=item_id, item_amount__gt=0
        )
        for slot in slots:
            total_available += get_available_slot_amount(slot)

        prices[item_id] = calculate_price(base_price, total_available, safe_stock)

    # Store as string-keyed JSON
    str_prices = {str(k): v for k, v in prices.items()}

    DailyPriceSnapshot.objects.update_or_create(
        day_index=target_day_index,
        defaults={
            "prices": str_prices,
            "status": DaySnapshotStatus.DRAFT,
        },
    )

    return str_prices


# ── Response helpers (local copies to avoid circular import) ─────────────────


def _ok(**overrides: Any) -> Dict[str, Any]:
    from .phase3_contract import VILLAGE_ECONOMY_RESULT_V1
    base: Dict[str, Any] = {
        "schema_version": VILLAGE_ECONOMY_RESULT_V1,
        "status": "SUCCEEDED",
        "result_code": ResultCode.OK,
        "replayed": False,
        "world_minute": 0,
        "asset_versions": {},
        "effects": {},
        "data": {},
        "event": {},
    }
    base.update(overrides)
    return base


def _fail(result_code: str, **overrides: Any) -> Dict[str, Any]:
    from .phase3_contract import VILLAGE_ECONOMY_RESULT_V1
    base: Dict[str, Any] = {
        "schema_version": VILLAGE_ECONOMY_RESULT_V1,
        "status": "FAILED",
        "result_code": result_code,
        "replayed": False,
        "world_minute": 0,
        "asset_versions": {},
        "effects": {},
        "data": {},
        "event": {},
    }
    base.update(overrides)
    return base


def _get_npc_obj(npc_id: int):
    """Get NpcDefinition or None."""
    from inventory.models import NpcDefinition
    try:
        return NpcDefinition.objects.get(agent_id=str(npc_id))
    except NpcDefinition.DoesNotExist:
        return None
