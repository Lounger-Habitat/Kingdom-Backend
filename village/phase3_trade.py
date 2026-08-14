"""Phase 3 trade service: orders, matching, reservations, and physical delivery.

Each handler follows the same signature as production handlers:
    (npc_id, params, world_minute, request_id) -> dict

Called by ``commit_economy_action`` after contract validation.
All mutations happen inside the outer ``transaction.atomic`` block.

Lock order (stable, deadlock-free):
  order(s) by order_id → delivery by delivery_id → slot by instance_id →
  inventory(s) by npc agent_id
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from django.db import models, transaction

from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot

from .models import NpcRuntimeState, NpcTradeDelivery, NpcTradeOrder
from .phase3_asset_service import (
    _fail,
    _ok,
    add_batch,
    get_available_slot_amount,
)
from .phase3_contract import DELIVERY_POINTS, ResultCode


# ── Buyer-specific helper ────────────────────────────────────────────────────


def _get_buyer_available_money(inventory: NpcGlobalInventory) -> int:
    """Return buyer's money minus ACTIVE delivery reserved money.

    ``get_available_money`` from asset_service tracks seller-side reservations.
    For BUY orders we need the buyer-side view: committed money across ACTIVE
    deliveries where *this* NPC is the buyer.
    """
    reserved = (
        NpcTradeDelivery.objects.filter(
            buyer_order__npc=inventory.npc, status="ACTIVE"
        ).aggregate(total=models.Sum("reserved_money"))["total"]
        or 0
    )
    return max(0, inventory.money - reserved)


# ── Slot lookup ──────────────────────────────────────────────────────────────


def _find_slot_with_item(
    inventory: NpcGlobalInventory, item_id: int, min_amount: int
) -> Optional[NpcGlobalInventorySlot]:
    """Find a slot with at least *min_amount* available (FEFO order).

    Returns a locked slot or ``None``.
    """
    slots = list(
        NpcGlobalInventorySlot.objects.select_for_update()
        .filter(bag=inventory, item_id=item_id, item_amount__gt=0)
        .order_by(
            models.F("expires_world_minute").asc(nulls_last=True),
            models.F("produced_world_minute").asc(nulls_last=True),
            "instance_id",
        )
    )
    for slot in slots:
        if get_available_slot_amount(slot) >= min_amount:
            return slot
    return None


# ── Matching algorithm ───────────────────────────────────────────────────────


def _try_match_order(
    new_order: NpcTradeOrder,
) -> Optional[NpcTradeOrder]:
    """Find the best matching opposite-side order.

    Priority: earliest ``created_at`` first.  Price compatibility:
    BUY.unit_price >= SELL.unit_price.

    Returns the matched order or ``None``.
    """
    opposite_side = "SELL" if new_order.side == "BUY" else "BUY"
    candidates = (
        NpcTradeOrder.objects.filter(
            side=opposite_side,
            item_id=new_order.item_id,
            amount=new_order.amount,
            delivery_point_code=new_order.delivery_point_code,
            status="OPEN",
        )
        .exclude(npc=new_order.npc)
        .order_by("created_at")
    )

    for candidate in candidates:
        # Price compatibility: BUY price >= SELL price
        if new_order.side == "BUY" and new_order.unit_price < candidate.unit_price:
            continue
        if new_order.side == "SELL" and candidate.unit_price < new_order.unit_price:
            continue
        return candidate
    return None


# ── Delivery creation ────────────────────────────────────────────────────────


def _create_delivery(
    buyer_order: NpcTradeOrder, seller_order: NpcTradeOrder
) -> Optional[NpcTradeDelivery]:
    """Create an ACTIVE delivery from two matched orders.

    Uses the earlier order's unit_price as the matched price.
    Returns ``None`` if orders can no longer be matched.
    """
    # Lock orders in stable order (by order_id string)
    oid_a, oid_b = sorted([str(buyer_order.order_id), str(seller_order.order_id)])
    NpcTradeOrder.objects.select_for_update().get(order_id=oid_a)
    NpcTradeOrder.objects.select_for_update().get(order_id=oid_b)

    # Re-fetch after lock
    buyer_order.refresh_from_db()
    seller_order.refresh_from_db()

    if buyer_order.status != "OPEN" or seller_order.status != "OPEN":
        return None

    # Find seller slot with enough available items
    seller_inv = NpcGlobalInventory.objects.select_for_update().get(
        npc=seller_order.npc
    )
    seller_slot = _find_slot_with_item(
        seller_inv, seller_order.item_id, seller_order.amount
    )
    if seller_slot is None:
        return None

    # Matched price: earlier order's unit_price
    if buyer_order.created_at <= seller_order.created_at:
        matched_price = buyer_order.unit_price
    else:
        matched_price = seller_order.unit_price

    delivery = NpcTradeDelivery.objects.create(
        buyer_order=buyer_order,
        seller_order=seller_order,
        seller_slot_instance_id=seller_slot.instance_id,
        reserved_amount=seller_order.amount,
        reserved_money=matched_price * seller_order.amount,
        delivery_point_code=buyer_order.delivery_point_code,
        expires_world_minute=max(
            buyer_order.expires_world_minute, seller_order.expires_world_minute
        ),
        status="ACTIVE",
    )

    # Update both orders
    buyer_order.matched_order = seller_order
    buyer_order.status = "MATCHED"
    buyer_order.save(update_fields=["matched_order", "status", "updated_at"])

    seller_order.matched_order = buyer_order
    seller_order.status = "MATCHED"
    seller_order.save(update_fields=["matched_order", "status", "updated_at"])

    return delivery


# ── CREATE_ORDER handler ─────────────────────────────────────────────────────


def _do_create_order(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """CREATE_ORDER: create buy/sell order, attempt matching."""
    side = params.get("side")
    item_id = params.get("item_id")
    amount = params.get("amount")
    unit_price = params.get("unit_price")
    delivery_point_code = params.get("delivery_point_code")
    expires_world_minute = params.get("expires_world_minute")
    expected_version = params.get("expected_asset_version")

    # ── param validation ─────────────────────────────────────────────────
    if side not in ("BUY", "SELL"):
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if not isinstance(item_id, int) or item_id <= 0:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if not isinstance(unit_price, int) or isinstance(unit_price, bool) or unit_price <= 0:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if delivery_point_code not in DELIVERY_POINTS:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if (
        not isinstance(expires_world_minute, int)
        or isinstance(expires_world_minute, bool)
        or expires_world_minute <= world_minute
    ):
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        # Lock inventory
        try:
            inventory = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        if expected_version is not None and inventory.asset_version != expected_version:
            return _fail(ResultCode.P3_ASSET_VERSION_CONFLICT)

        # ── resource check ───────────────────────────────────────────────
        if side == "SELL":
            total_available = 0
            slots = NpcGlobalInventorySlot.objects.select_for_update().filter(
                bag=inventory, item_id=item_id, item_amount__gt=0
            )
            for slot in slots:
                total_available += get_available_slot_amount(slot)
            if total_available < amount:
                return _fail(ResultCode.P3_INSUFFICIENT_ITEM)
        else:  # BUY
            if _get_buyer_available_money(inventory) < amount * unit_price:
                return _fail(ResultCode.P3_INSUFFICIENT_MONEY)

        # ── create order ─────────────────────────────────────────────────
        npc_obj = inventory.npc
        order = NpcTradeOrder.objects.create(
            request_id=request_id,
            side=side,
            npc=npc_obj,
            item_id=item_id,
            amount=amount,
            unit_price=unit_price,
            delivery_point_code=delivery_point_code,
            expires_world_minute=expires_world_minute,
            status="OPEN",
        )

        # ── attempt matching ─────────────────────────────────────────────
        delivery = None
        matched_order = _try_match_order(order)
        if matched_order is not None:
            if order.side == "BUY":
                delivery = _create_delivery(order, matched_order)
            else:
                delivery = _create_delivery(matched_order, order)

        # ── bump asset versions ──────────────────────────────────────────
        inventory.asset_version += 1
        inventory.save(update_fields=["asset_version", "updated_at"])
        asset_versions: Dict[str, int] = {str(npc_id): inventory.asset_version}

        if delivery is not None and matched_order is not None:
            other_inv = NpcGlobalInventory.objects.select_for_update().get(
                npc=matched_order.npc
            )
            other_inv.asset_version += 1
            other_inv.save(update_fields=["asset_version", "updated_at"])
            asset_versions[str(matched_order.npc.agent_id)] = other_inv.asset_version

    # ── build response ───────────────────────────────────────────────────
    effects: Dict[str, Any] = {
        "order": {
            "order_id": str(order.order_id),
            "side": side,
            "item_id": item_id,
            "amount": amount,
            "unit_price": unit_price,
            "status": order.status,
        },
    }
    if delivery is not None:
        effects["delivery"] = {
            "delivery_id": str(delivery.delivery_id),
            "status": delivery.status,
            "reserved_amount": delivery.reserved_amount,
            "reserved_money": delivery.reserved_money,
        }

    return _ok(asset_versions=asset_versions, effects=effects, external_delta=0)


# ── CANCEL_ORDER handler ─────────────────────────────────────────────────────


def _do_cancel_order(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """CANCEL_ORDER: cancel open or matched order."""
    order_id = params.get("order_id")
    if order_id is None:
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        try:
            order = NpcTradeOrder.objects.select_for_update().get(order_id=order_id)
        except NpcTradeOrder.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Ownership check
        if str(order.npc.agent_id) != str(npc_id):
            return _fail(ResultCode.P3_FORBIDDEN_ACTOR)

        asset_versions: Dict[str, int] = {}

        if order.status == "OPEN":
            order.status = "CANCELLED"
            order.save(update_fields=["status", "updated_at"])

        elif order.status == "MATCHED":
            # Lock matched order in stable order
            matched = NpcTradeOrder.objects.select_for_update().get(
                order_id=order.matched_order_id
            )

            # Cancel the ACTIVE delivery
            delivery = NpcTradeDelivery.objects.filter(
                models.Q(buyer_order=order) | models.Q(seller_order=order),
                status="ACTIVE",
            ).first()
            if delivery is not None:
                delivery.status = "CANCELLED"
                delivery.save(update_fields=["status", "updated_at"])

            # Cancel both orders and clear cross-references
            order.status = "CANCELLED"
            order.matched_order = None
            order.save(update_fields=["status", "matched_order", "updated_at"])

            matched.status = "CANCELLED"
            matched.matched_order = None
            matched.save(update_fields=["status", "matched_order", "updated_at"])

            # Bump matched NPC's asset version
            matched_inv = NpcGlobalInventory.objects.select_for_update().get(
                npc=matched.npc
            )
            matched_inv.asset_version += 1
            matched_inv.save(update_fields=["asset_version", "updated_at"])
            asset_versions[str(matched.npc.agent_id)] = matched_inv.asset_version

        else:
            # FILLED / EXPIRED -- cannot cancel
            return _fail(ResultCode.P3_ORDER_NOT_OPEN)

        # Bump actor's asset version
        inventory = NpcGlobalInventory.objects.select_for_update().get(
            npc=order.npc
        )
        inventory.asset_version += 1
        inventory.save(update_fields=["asset_version", "updated_at"])
        asset_versions[str(npc_id)] = inventory.asset_version

    return _ok(
        asset_versions=asset_versions,
        effects={
            "order_cancelled": {
                "order_id": str(order.order_id),
                "status": "CANCELLED",
            },
        },
        external_delta=0,
    )


# ── START_DELIVERY handler ───────────────────────────────────────────────────


def _do_start_delivery(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    """START_DELIVERY: physical delivery when buyer+seller at same point."""
    delivery_id = params.get("delivery_id")
    buyer_npc_id = params.get("buyer_npc_id")
    seller_npc_id = params.get("seller_npc_id")
    delivery_point_code = params.get("delivery_point_code")

    # Actor must be the seller
    if str(npc_id) != str(seller_npc_id):
        return _fail(ResultCode.P3_FORBIDDEN_ACTOR)

    with transaction.atomic():
        # Lock delivery
        try:
            delivery = NpcTradeDelivery.objects.select_for_update().get(
                delivery_id=delivery_id
            )
        except NpcTradeDelivery.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        if delivery.status != "ACTIVE":
            return _fail(ResultCode.P3_DELIVERY_NOT_READY)

        # Validate participant and point match delivery record
        if str(delivery.buyer_order.npc.agent_id) != str(buyer_npc_id):
            return _fail(ResultCode.P3_INVALID_PARAMS)
        if str(delivery.seller_order.npc.agent_id) != str(seller_npc_id):
            return _fail(ResultCode.P3_INVALID_PARAMS)
        if delivery.delivery_point_code != delivery_point_code:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        # Both NPCs must be at the delivery point
        try:
            buyer_rt = NpcRuntimeState.objects.get(npc__agent_id=str(buyer_npc_id))
            seller_rt = NpcRuntimeState.objects.get(npc__agent_id=str(seller_npc_id))
        except NpcRuntimeState.DoesNotExist:
            return _fail(ResultCode.P3_DELIVERY_NOT_READY)

        if buyer_rt.current_location_code != delivery_point_code:
            return _fail(ResultCode.P3_DELIVERY_NOT_READY)
        if seller_rt.current_location_code != delivery_point_code:
            return _fail(ResultCode.P3_DELIVERY_NOT_READY)

        # Lock orders in stable order
        oid_a, oid_b = sorted(
            [str(delivery.buyer_order_id), str(delivery.seller_order_id)]
        )
        NpcTradeOrder.objects.select_for_update().get(order_id=oid_a)
        NpcTradeOrder.objects.select_for_update().get(order_id=oid_b)

        # Re-verify delivery status after locking orders
        delivery.refresh_from_db()
        if delivery.status != "ACTIVE":
            return _fail(ResultCode.P3_DELIVERY_NOT_READY)

        # Lock seller slot
        seller_slot = NpcGlobalInventorySlot.objects.select_for_update().filter(
            instance_id=delivery.seller_slot_instance_id
        ).first()
        if seller_slot is None:
            return _fail(ResultCode.P3_INSUFFICIENT_ITEM)

        # Lock both inventories in stable order (by agent_id)
        nid_a, nid_b = sorted([str(buyer_npc_id), str(seller_npc_id)])
        inv_a = NpcGlobalInventory.objects.select_for_update().get(
            npc__agent_id=nid_a
        )
        inv_b = NpcGlobalInventory.objects.select_for_update().get(
            npc__agent_id=nid_b
        )
        buyer_inv = inv_a if nid_a == str(buyer_npc_id) else inv_b
        seller_inv = inv_b if nid_a == str(buyer_npc_id) else inv_a

        # ── re-verify resources ──────────────────────────────────────────
        if seller_slot.item_amount < delivery.reserved_amount:
            return _fail(ResultCode.P3_INSUFFICIENT_ITEM)
        if buyer_inv.money < delivery.reserved_money:
            return _fail(ResultCode.P3_INSUFFICIENT_MONEY)
        if (
            seller_slot.expires_world_minute is not None
            and seller_slot.expires_world_minute <= world_minute
        ):
            return _fail(ResultCode.P3_INSUFFICIENT_ITEM)

        # Capture for effects before mutation
        item_id = delivery.seller_order.item_id
        transfer_amount = delivery.reserved_amount
        transfer_money = delivery.reserved_money

        # ── execute transfer ─────────────────────────────────────────────
        # Seller deduct items
        seller_slot.item_amount -= transfer_amount
        seller_slot.save(update_fields=["item_amount"])

        # Buyer add same-batch items (new slot, preserving batch properties)
        add_batch(
            buyer_inv,
            item_id,
            transfer_amount,
            seller_slot.produced_world_minute or world_minute,
            seller_slot.expires_world_minute,
            request_id,
        )

        # Money transfer: buyer pays seller
        buyer_inv.money -= transfer_money
        buyer_inv.save(update_fields=["money"])
        seller_inv.money += transfer_money
        seller_inv.save(update_fields=["money"])

        # ── update statuses ──────────────────────────────────────────────
        delivery.status = "COMPLETED"
        delivery.save(update_fields=["status", "updated_at"])

        delivery.buyer_order.status = "FILLED"
        delivery.buyer_order.save(update_fields=["status", "updated_at"])

        delivery.seller_order.status = "FILLED"
        delivery.seller_order.save(update_fields=["status", "updated_at"])

        # Bump both asset versions
        buyer_inv.asset_version += 1
        buyer_inv.save(update_fields=["asset_version", "updated_at"])
        seller_inv.asset_version += 1
        seller_inv.save(update_fields=["asset_version", "updated_at"])

    return _ok(
        asset_versions={
            str(buyer_npc_id): buyer_inv.asset_version,
            str(seller_npc_id): seller_inv.asset_version,
        },
        effects={
            "delivery_completed": {
                "delivery_id": str(delivery_id),
                "item_id": item_id,
                "amount": transfer_amount,
                "money_transferred": transfer_money,
            },
        },
        external_delta=0,
    )
