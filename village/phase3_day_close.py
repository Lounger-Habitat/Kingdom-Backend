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

from game_time.models import GlobalGameTimeState
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
from .phase4_social import breach_due_commitments, social_enabled


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
    """GENERAL_STORE 库存告警状态，与日间查询共用同一判定（二跑复盘 R7）。

    只查标签；按 SKU 明细见 ``get_store_availability``。
    """
    from .models import VillageStore
    from .phase3_direct_trade import store_inventory_status

    try:
        store = VillageStore.objects.get(store_code="GENERAL_STORE")
    except VillageStore.DoesNotExist:
        return StoreStatus.OUT_OF_STOCK.value
    return store_inventory_status(store, world_minute)[0]


def get_store_availability(world_minute: int) -> Dict[int, int]:
    """GENERAL_STORE 按 SKU 的可售数量，与 get_store_status 同一次判定。"""
    from .models import VillageStore
    from .phase3_direct_trade import store_inventory_status

    try:
        store = VillageStore.objects.get(store_code="GENERAL_STORE")
    except VillageStore.DoesNotExist:
        return {}
    return store_inventory_status(store, world_minute)[1]


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
        # Keep the authority lock ahead of account locks, as in other settlements.
        clock = GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
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

        all_changes["commitments_breached"] = (
            breach_due_commitments(day_end_minute, time_revision=world_minute)
            if social_enabled() else 0
        )

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
        store_available_by_item = get_store_availability(world_minute)

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
        all_changes["store_available_by_item"] = store_available_by_item

        # ── Step 6: Update account and save receipt ──────────────────────────
        # Traveler sales can change this account through another model instance.
        account.refresh_from_db(fields=["balance", "asset_version"])
        from .public_finance import settle_mark_daily_salary
        salary = settle_mark_daily_salary(account, day_index, world_minute, request_id)
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
                "store_available_by_item": store_available_by_item,
                "traveler_sales": sales_result,
                "spoilage": spoilage_result,
                "expired": expire_result,
                "price_snapshot_day": day_index + 1,
                "ledger_summaries": ledger_summaries,
                "salary": salary,
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
            # Keep the source of NPC money increases on the public event too.
            # The ledger already records these values, but exposing them here
            # makes a day-close balance change auditable from the event alone.
            effects={
                "day_index": day_index,
                "traveler_sales": sales_result,
                "external_delta": total_external_delta,
            },
            visibility="PUBLIC",
            public_data={
                "day_index": day_index,
                "restaurant_status": restaurant_status,
                "store_status": store_status,
                "store_available_by_item": store_available_by_item,
                "traveler_sales": sales_result,
                "external_delta": total_external_delta,
            },
            player_summary="日结已完成。",
            hub_summary="日结已完成。",
        )
        from .phase4_visibility import enqueue_event_deliveries
        enqueue_event_deliveries(event)
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
                "PUBLIC_FUND": {"money": sales_result.get("fee_amount", 0) - salary["paid"]},
                "10013": {"money": salary["paid"]},
                "salary": salary,
                "STORE:RESTAURANT_STORE": {"money": sales_result.get("store_net_amount", 0)},
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

        # Phase 5 is an optional projection in the same day-close transaction.
        # When disabled this import/call is a no-op and Phase 3/4 semantics stay unchanged.
        if VillageSimulationConfig.objects.filter(
            config_key="default", village_story_enabled=True
        ).exists():
            from .phase5_story import process_day_close
            story_result = process_day_close(day_index, world_minute, request_id)
            result["story"] = story_result.get("data", {})

        if clock is not None:
            from .daily_events import evaluate_daily_events
            evaluate_daily_events(clock.world_minute)

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
    from .models import VillageStoreInventorySlot
    from .lucy_restaurant import store_available
    return sum(store_available(s, day_end_minute) for s in VillageStoreInventorySlot.objects.filter(
        store_id="RESTAURANT_STORE", listing__status="ACTIVE", item_amount__gt=0))



def _traveler_sales(
    day_index: int,
    day_end_minute: int,
    request_id: uuid.UUID,
) -> Tuple[Dict[str, Any], int]:
    from .models import VillageStore, VillageStoreAccount, VillageStoreListing, VillageStoreInventorySlot
    from .lucy_restaurant import store_available
    from .lucy_cooking import fixed_price
    from .phase3_direct_trade import _compute_fee
    store = VillageStore.objects.select_for_update().filter(pk="RESTAURANT_STORE", status="OPEN").first()
    if not store:
        return {"sold": 0, "revenue": 0, "fee_amount": 0}, 0
    account = VillageStoreAccount.objects.select_for_update().filter(store=store).first()
    fund = VillageAccount.objects.select_for_update().filter(account_code="PUBLIC_FUND").first()
    if not account or not fund:
        return {"sold": 0, "revenue": 0, "fee_amount": 0}, 0
    sold = gross = fees = 0
    rows = []
    slots = VillageStoreInventorySlot.objects.select_for_update().select_related("listing").filter(
        store=store, listing__status="ACTIVE", listing__public_meal_code="", listing__unit_price__gt=0,
        item_amount__gt=0).order_by(models.F("expires_world_minute").asc(nulls_last=True), "instance_id")
    for slot in slots:
        available = store_available(slot, day_end_minute)
        try:
            price = fixed_price(slot.item_id, slot.item_attributes)
            fee, net = _compute_fee(price, store.transaction_fee_bps, store.minimum_fee)
        except ValueError:
            continue
        if price != slot.listing.unit_price:
            continue
        take = min(available, TRAVELER_MAX_SALES - sold)
        if take <= 0:
            continue
        slot.item_amount -= take
        slot.save(update_fields=["item_amount"])
        sold += take
        gross += take * price
        fees += take * fee
        rows.append({"listing_id": str(slot.listing_id), "amount": take, "unit_price": price})
        if not any(store_available(s, day_end_minute) for s in VillageStoreInventorySlot.objects.filter(listing=slot.listing)):
            VillageStoreListing.objects.filter(pk=slot.listing_id).update(status="SOLD_OUT")
        if sold >= TRAVELER_MAX_SALES:
            break
    if sold:
        account.balance += gross - fees
        account.account_version += 1
        account.save(update_fields=["balance", "account_version", "updated_at"])
        fund.balance += fees
        fund.asset_version += 1
        fund.save(update_fields=["balance", "asset_version", "updated_at"])
        store.inventory_version += 1
        store.save(update_fields=["inventory_version", "updated_at"])
    return {"sold": sold, "revenue": gross, "fee_amount": fees, "store_net_amount": gross-fees,
            "income_account": "STORE:RESTAURANT_STORE", "sales": rows}, gross



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

        from .caravan import available_village_stock
        total_available = available_village_stock(item_id, world_minute)

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
