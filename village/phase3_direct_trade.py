"""Phase 3 direct trade & independent store service.

Implements the 7 Phase 3 direct-trade commands (PROPOSE_TRADE,
RESPOND_TRADE, CANCEL_TRADE_OFFER, LIST_STORE_ITEM, UPDATE_STORE_PRICE,
DELIST_STORE_ITEM, BUY_FROM_STORE) plus the read-only store-inventory query
helper and the legacy order-system migration.

Each handler follows the same signature as the production / public-economy
handlers::

    (npc_id, params, world_minute, request_id) -> dict

Called by ``commit_economy_action`` after contract validation. Business errors
roll back the handler savepoint. The caller separately records a failed
receipt and the terminal EXPIRED status without retaining asset mutations.

Lock order (stable, deadlock-free) -- always acquire in this sequence:

    1. VillageTradeOffer            by offer_id
    2. VillageStore                 by store_code
    3. VillageStoreListing          by listing_id
    4. VillageStoreAccount          by store_code
    5. VillageAccount (PUBLIC_FUND) by account_code
    6. NpcGlobalInventory           by agent_id (``sorted`` when multiple)
    7. NpcGlobalInventorySlot / VillageStoreInventorySlot
                                    by instance_id (``sorted`` when multiple)

``BUY_FROM_STORE`` resolves the store_code from the listing first (the FK is
immutable), then locks store -> listing to honour the store-before-listing
order and avoid an AB-BA deadlock with ``LIST_STORE_ITEM``.

Money conservation for store purchases (design 7.5)::

    buyer_money_delta + store_account_delta + public_fund_delta == 0
    external_delta == 0

Fee formula (design 7.4, floor then minimum, never above gross)::

    gross_amount  = listing.unit_price * amount
    raw_fee       = floor(gross_amount * transaction_fee_bps / 10000)
    fee_amount    = min(gross_amount, max(minimum_fee, raw_fee))
    store_net     = gross_amount - fee_amount
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from django.db import models, transaction

from inventory.models import NpcDefinition, NpcGlobalInventory, NpcGlobalInventorySlot
from items.models import Item, ItemType

from .models import (
    EconomicLedgerEntry,
    NpcCommitment,
    NpcRuntimeState,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageStore,
    VillageStoreAccount,
    VillageStoreInventorySlot,
    VillageStoreListing,
    VillageTradeOffer,
    VillageWorldEvent,
)
from .phase3_asset_service import (
    BATCH_ITEM_ATTRIBUTES,
    _fail,
    _ok,
    add_batch,
    get_available_slot_amount,
)
from .phase3_catalog import BASE_PRICES, SAFE_STOCKS
from .phase3_contract import (
    ListingStatus,
    PriceMode,
    ResultCode,
    Side,
    StoreOperationalStatus,
    StoreStatus,
    TradeOfferStatus,
)

# ── Command set (also used by the asset-service feature gate) ────────────────

DIRECT_TRADE_COMMANDS = frozenset({
    "PROPOSE_TRADE",
    "RESPOND_TRADE",
    "CANCEL_TRADE_OFFER",
    "LIST_STORE_ITEM",
    "UPDATE_STORE_PRICE",
    "DELIST_STORE_ITEM",
    "BUY_FROM_STORE",
})

# Store operational status that permits trading.
_TRADABLE_STORE_STATUSES = frozenset({StoreOperationalStatus.OPEN.value})

# ── Small helpers ────────────────────────────────────────────────────────────


def _parse_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _trade_item_name(item_id: int) -> str | None:
    return Item.objects.filter(item_id=item_id, is_deleted=False).values_list(
        "item_name", flat=True
    ).first()


def _get_buyer_available_money(inventory: NpcGlobalInventory) -> int:
    """Buyer-side available money (money minus ACTIVE-delivery reservations)."""
    reserved = (
        NpcTradeDelivery.objects.filter(
            buyer_order__npc=inventory.npc, status="ACTIVE"
        ).aggregate(total=models.Sum("reserved_money"))["total"]
        or 0
    )
    return max(0, inventory.money - reserved)


def general_store_tool_sale_reason(item_type, max_use_count, remaining_uses):
    """Fixed-price automatic buyback accepts only tools at configured full durability.

    Negotiated personal trades and operator listings retain their own pricing.
    """
    if item_type not in {
        ItemType.HOE_TOOL, ItemType.CHOP_TOOL, ItemType.BREAK_TOOL,
        ItemType.REAP_TOOL, ItemType.WATER_TOOL, ItemType.COLLECT_TOOL,
    }:
        return None
    if remaining_uses is None:
        return "TOOL_DURABILITY_UNKNOWN"
    if max_use_count == -1 and remaining_uses == -1:
        return None
    if max_use_count <= 0 or remaining_uses < 0 or remaining_uses > max_use_count:
        return "TOOL_DURABILITY_INVALID"
    if remaining_uses == 0:
        return "TOOL_BROKEN"
    if remaining_uses < max_use_count:
        return "TOOL_NOT_FULL_DURABILITY"
    return None


def _consume_personal_fefo_available(
    inventory: NpcGlobalInventory,
    item_id: int,
    amount: int,
    world_minute: int,
    *, store_purchase_item: Optional[Item] = None,
) -> List[Tuple[NpcGlobalInventorySlot, int]]:
    """Consume *amount* available items from an NPC inventory (FEFO).

    Skips expired batches and ACTIVE-delivery reservations.  Reduces
    ``item_amount`` in place.  Raises ``ValueError("INSUFFICIENT_ITEM")`` if
    the unreserved/unexpired total is less than *amount*.
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
    # Validate before any mutation: compute total unreserved/unexpired amount
    # and fail early so a failed settlement never partially deducts items.
    total_available = 0
    eligible_slots = []
    tool_rejection = None
    for slot in slots:
        if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
            continue
        available = get_available_slot_amount(slot, world_minute)
        if available <= 0:
            continue
        if store_purchase_item is not None:
            reason = general_store_tool_sale_reason(
                store_purchase_item.item_type, store_purchase_item.max_use_count, slot.remaining_uses,
            )
            if reason:
                tool_rejection = tool_rejection or reason
                continue
        eligible_slots.append(slot)
        total_available += available
    if total_available < amount:
        raise ValueError(tool_rejection or "INSUFFICIENT_ITEM")

    consumed: List[Tuple[NpcGlobalInventorySlot, int]] = []
    remaining = amount
    for slot in eligible_slots:
        if remaining <= 0:
            break
        if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
            continue
        available = get_available_slot_amount(slot, world_minute)
        if available <= 0:
            continue
        take = min(available, remaining)
        slot.item_amount -= take
        slot.save(update_fields=["item_amount"])
        consumed.append((slot, take))
        remaining -= take
    return consumed


def _consume_store_fefo_available(
    store: VillageStore,
    listing: VillageStoreListing,
    amount: int,
    world_minute: int,
) -> List[Tuple[VillageStoreInventorySlot, int]]:
    """Consume *amount* available items from a store listing's batches (FEFO)."""
    from .lucy_restaurant import store_available
    slots = list(
        VillageStoreInventorySlot.objects.select_for_update()
        .filter(store=store, listing=listing, item_amount__gt=0)
        .order_by(
            models.F("expires_world_minute").asc(nulls_last=True),
            models.F("produced_world_minute").asc(nulls_last=True),
            "instance_id",
        )
    )
    # Validate before any mutation (mirror the personal-inventory helper).
    total_available = 0
    for slot in slots:
        if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
            continue
        total_available += store_available(slot, world_minute)
    if total_available < amount:
        raise ValueError("INSUFFICIENT_ITEM")

    consumed: List[Tuple[VillageStoreInventorySlot, int]] = []
    remaining = amount
    for slot in slots:
        if remaining <= 0:
            break
        if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
            continue
        available = store_available(slot, world_minute)
        if available <= 0:
            continue
        take = min(available, remaining)
        slot.item_amount -= take
        slot.save(update_fields=["item_amount"])
        consumed.append((slot, take))
        remaining -= take
    return consumed


def _compute_fee(
    gross_amount: int, transaction_fee_bps: int, minimum_fee: int
) -> Tuple[int, int]:
    """Return ``(fee_amount, store_net_amount)``.

    Raises ``ValueError("INVALID_FEE_CONFIG")`` when the fee policy is illegal.
    Python ints are unbounded, so no integer overflow is possible; the range
    check below is the authoritative guard mirrored by the DB constraints.
    """
    if not isinstance(transaction_fee_bps, int) or isinstance(transaction_fee_bps, bool):
        raise ValueError("INVALID_FEE_CONFIG")
    if not isinstance(minimum_fee, int) or isinstance(minimum_fee, bool):
        raise ValueError("INVALID_FEE_CONFIG")
    if not (0 <= transaction_fee_bps <= 10000):
        raise ValueError("INVALID_FEE_CONFIG")
    if minimum_fee < 0:
        raise ValueError("INVALID_FEE_CONFIG")
    raw_fee = (gross_amount * transaction_fee_bps) // 10000  # floor
    fee_amount = min(gross_amount, max(minimum_fee, raw_fee))
    store_net_amount = gross_amount - fee_amount
    return fee_amount, store_net_amount


def store_inventory_status(store: VillageStore, world_minute: int) -> Tuple[str, Dict[int, int]]:
    """(inventory_status, available_by_item)——商店库存告警的单一判定实现。

    二跑复盘（R7）：OUT_OF_STOCK 必须表示"全店没有任何可售库存"；
    有货但 SAFE_STOCKS 任一 SKU 低于安全线只记 LOW_STOCK（库存告警，不是关门）。
    日间查询（query_store_inventory）与日结事件（phase3_day_close.get_store_status）
    共用本函数，保证两个入口同语义，不再出现"白天 LOW_STOCK、晚上 OUT_OF_STOCK"。
    """
    available_by_item: Dict[int, int] = {}
    slots = VillageStoreInventorySlot.objects.filter(store=store, item_amount__gt=0)
    if store.store_code == "RESTAURANT_STORE":
        slots = slots.filter(listing__status="ACTIVE")
    for slot in slots:
        if (slot.item_attributes or {}).get("freshness_unknown"):
            continue
        if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
            continue
        available = max(0, slot.item_amount - slot.reserved_amount)
        if available == 0:
            continue
        available_by_item[slot.item_id] = available_by_item.get(slot.item_id, 0) + available

    if not available_by_item:
        return StoreStatus.OUT_OF_STOCK.value, available_by_item
    if store.store_code == "RESTAURANT_STORE":
        return StoreStatus.NORMAL.value, available_by_item
    shortfall = any(
        available_by_item.get(item_id, 0) < safe and safe > 0
        for item_id, safe in SAFE_STOCKS.items()
    )
    return (StoreStatus.LOW_STOCK.value if shortfall else StoreStatus.NORMAL.value), available_by_item


def _store_inventory_status(store: VillageStore, world_minute: int) -> str:
    """状态标签（兼容入口；phase3_views 等仍按此名引用）。"""
    return store_inventory_status(store, world_minute)[0]


def _resolve_final_unit_price(
    price_mode: Any, custom_unit_price: Any, item_id: int
) -> Optional[int]:
    """Resolve the final unit price; returns None on invalid params."""
    if price_mode == PriceMode.ORIGINAL:
        if custom_unit_price is not None:
            return None
        # Keep ORIGINAL compatible for catalog items, while allowing a valid
        # inventory item outside that catalog to use its item configuration as
        # a reference price. Agents should use CUSTOM when deciding prices.
        base = BASE_PRICES.get(item_id)
        if base is None:
            base = Item.objects.filter(
                item_id=item_id, is_deleted=False
            ).values_list("item_price", flat=True).first()
        if base is None or not isinstance(base, int) or base <= 0:
            return None
        return base
    if price_mode == PriceMode.CUSTOM:
        if not _is_positive_int(custom_unit_price):
            return None
        return custom_unit_price
    return None


# ── PROPOSE_TRADE ────────────────────────────────────────────────────────────


def _do_sell_to_store(npc_id, params, world_minute, request_id):
    """Immediate fixed-value sale; the store buys without an operator response.

    Kept behind the existing SELL + store_code entry so existing NPC trade
    permissions remain sufficient. No pending offer or personal buyer exists.
    """
    seller = NpcDefinition.objects.filter(
        agent_id=str(npc_id), is_test_only=False,
    ).first()
    if (str(npc_id) not in {"10011", "10012", "10013", "10014"}
            or seller is None or params.get("store_code") != "GENERAL_STORE"
            or ("target_npc_id" in params and str(params["target_npc_id"]) != "10015")
            or params.get("commitment_id")):
        return _fail(ResultCode.TRADE_STORE_FORBIDDEN)
    store = VillageStore.objects.select_for_update().filter(
        store_code="GENERAL_STORE", operator_npc__agent_id="10015",
        operator_npc__is_test_only=False,
    ).first()
    if store is None:
        return _fail(ResultCode.TRADE_STORE_FORBIDDEN)
    if store.status != StoreOperationalStatus.OPEN.value:
        return _fail(ResultCode.TRADE_STORE_CLOSED)
    runtime = NpcRuntimeState.objects.filter(npc=seller).first()
    if (runtime is None or runtime.health_fp <= 300
            or runtime.action_status in {"AWAY", "SLEEPING"}
            or runtime.current_action == "SLEEP"
            or (runtime.away_until_world_minute is not None
                and runtime.away_until_world_minute > world_minute)):
        return _fail(ResultCode.P3_FORBIDDEN_ACTOR)

    if "items" in params:
        rows = params["items"]
        if ("item_id" in params or "amount" in params
                or not isinstance(rows, list) or not rows):
            return _fail(ResultCode.P3_INVALID_PARAMS)
    else:
        rows = [{"item_id": params.get("item_id"), "amount": params.get("amount")}]
    quantities = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"item_id", "amount"}
                or not _is_positive_int(row["item_id"])
                or not _is_positive_int(row["amount"])):
            return _fail(ResultCode.P3_INVALID_PARAMS)
        quantities[row["item_id"]] = quantities.get(row["item_id"], 0) + row["amount"]
    priced_items = []
    item_configs = {}
    for item_id, amount in sorted(quantities.items()):
        item = Item.objects.filter(item_id=item_id, is_deleted=False).first()
        if item is None or not _is_positive_int(item.item_price):
            return _fail(ResultCode.P3_INVALID_PARAMS, data={
                "failure_kind": "ITEM_VALUE_UNAVAILABLE", "unavailable_item_id": item_id,
            })
        priced_items.append({"item_id": item_id, "item_name": item.item_name,
                             "amount": amount, "unit_price": item.item_price,
                             "gold_amount": item.item_price * amount})
        item_configs[item_id] = item
    # The canonical item value is authoritative, including when a legacy
    # caller supplies gold_amount. Retail markups, ratings and sell_percentage
    # do not change this fixed-value store purchase.
    total = sum(row["gold_amount"] for row in priced_items)
    account = VillageStoreAccount.objects.select_for_update().filter(store=store).first()
    inventory = NpcGlobalInventory.objects.select_for_update().get(npc=seller)
    expected = params.get("expected_seller_asset_version")
    if expected is not None and expected != inventory.asset_version:
        return _fail(ResultCode.TRADE_ASSET_VERSION_CONFLICT)
    sale_data = {
        "sale_mode": "IMMEDIATE_STORE", "store_code": store.pk,
        "payment_source": "STORE", "seller_npc_id": str(npc_id),
        "items": priced_items, "price_source": "ITEM_PRICE", "gold_amount": total,
    }
    # Preserve scalar result fields for existing single-item callers.
    if len(priced_items) == 1:
        sale_data.update(priced_items[0])
    if account is None or account.balance < total:
        return _fail(ResultCode.TRADE_INSUFFICIENT_MONEY, data={**sale_data,
            "required_amount": total, "available_amount": account.balance if account else 0, "payer": "STORE"})
    for row in priced_items:
        item_id, amount = row["item_id"], row["amount"]
        try:
            consumed = _consume_personal_fefo_available(
                inventory, item_id, amount, world_minute, store_purchase_item=item_configs[item_id],
            )
        except ValueError as exc:
            # The asset-service savepoint rolls back every earlier item too.
            tool_rejected = str(exc).startswith("TOOL_")
            return _fail(ResultCode.TRADE_ITEM_NOT_FOR_SALE if tool_rejected else ResultCode.TRADE_INSUFFICIENT_ITEM, data={
                **sale_data, "unavailable_item_id": item_id,
                **({"failure_kind": str(exc), "tool_buyback_policy": "FULL_DURABILITY_ONLY"} if tool_rejected else {}),
            })
        for slot, taken in consumed:
            VillageStoreInventorySlot.objects.create(
                store=store, item_id=item_id, item_amount=taken,
                produced_world_minute=slot.produced_world_minute,
                expires_world_minute=slot.expires_world_minute, remaining_uses=slot.remaining_uses,
                source_npc=seller, source_slot_instance_id=slot.instance_id,
                item_attributes={key: getattr(slot, key) for key in BATCH_ITEM_ATTRIBUTES},
            )
    account.balance -= total
    account.account_version += 1
    account.save(update_fields=["balance", "account_version", "updated_at"])
    inventory.money += total
    inventory.asset_version += 1
    inventory.save(update_fields=["money", "asset_version", "updated_at"])
    store.inventory_version += 1
    store.save(update_fields=["inventory_version", "updated_at"])
    return _ok(
        data={**sale_data, "status": "COMPLETED",
              "personal_wallet": {"delta": total, "balance_after": inventory.money}},
        effects={"STORE:" + store.pk: {"money": -total, **{str(k): v for k, v in quantities.items()}},
                 str(npc_id): {"money": total, **{str(k): -v for k, v in quantities.items()}}},
        asset_versions={str(npc_id): inventory.asset_version}, external_delta=0,
        participant_npc_ids=[str(npc_id), "10015"],
    )


def _do_propose_trade(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    target_npc_id = params.get("target_npc_id")
    side = str(params.get("side", Side.BUY.value)).upper()
    item_id = params.get("item_id")
    amount = params.get("amount")
    gold_amount = params.get("gold_amount")
    conversation_id = params.get("conversation_id")
    expected_seller_asset_version = params.get("expected_seller_asset_version")
    expires_world_minute = params.get("expires_world_minute")
    commitment_id = _parse_uuid(params.get("commitment_id"))

    if params.get("store_code") and side == Side.SELL.value:
        return _do_sell_to_store(npc_id, params, world_minute, request_id)

    if "items" in params:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if target_npc_id is None or str(target_npc_id) == str(npc_id):
        return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)
    if side not in {Side.BUY.value, Side.SELL.value}:
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if not _is_positive_int(item_id):
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if not _is_positive_int(amount):
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if not _is_positive_int(gold_amount):
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if (
        not isinstance(expires_world_minute, int)
        or isinstance(expires_world_minute, bool)
        or expires_world_minute <= world_minute
    ):
        return _fail(ResultCode.P3_INVALID_PARAMS)

    target = NpcDefinition.objects.filter(agent_id=str(target_npc_id)).first()
    proposer = NpcDefinition.objects.filter(agent_id=str(npc_id)).first()
    if target is None or proposer is None:
        return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)

    buyer, seller = (proposer, target) if side == Side.BUY.value else (target, proposer)

    proposer_rt = NpcRuntimeState.objects.filter(npc=proposer).first()
    if proposer_rt is None:
        return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)

    commitment = None
    if commitment_id is not None:
        from .phase4_social import social_enabled
        if not social_enabled():
            return _fail("VILLAGE_SOCIAL_DISABLED")
        commitment = NpcCommitment.objects.select_for_update().filter(
            commitment_id=commitment_id,
            commitment_type="DELIVERY",
            status="OPEN",
            promisor_npc=seller,
            beneficiary_npc=buyer,
        ).first()
        if commitment is None:
            return _fail("COMMITMENT_NOT_FOUND")
        target = commitment.target or {}
        if (
            target.get("item_id") != item_id
            or target.get("amount") != amount
            or target.get("gold_amount") != gold_amount
            or commitment.due_world_minute < world_minute
        ):
            return _fail("COMMITMENT_TARGET_MISMATCH")

    buying_store = None
    if params.get("store_code"):
        from .caravan import owned_store
        store_buy = str(npc_id) == "10015" and side == Side.BUY.value
        if not store_buy or commitment or params["store_code"] != "GENERAL_STORE":
            return _fail(ResultCode.TRADE_STORE_FORBIDDEN)
        buying_store = owned_store("10015", params["store_code"])
        if buying_store is None:
            return _fail(ResultCode.TRADE_STORE_FORBIDDEN)
        if buying_store.status != StoreOperationalStatus.OPEN.value:
            return _fail(ResultCode.TRADE_STORE_CLOSED)
    offer = VillageTradeOffer.objects.create(
        buying_store=buying_store,
        buyer_npc=buyer,
        seller_npc=seller,
        proposer_npc=proposer,
        commitment=commitment,
        item_id=item_id,
        amount=amount,
        gold_amount=gold_amount,
        conversation_id=conversation_id,
        # Keep the Unity-synchronised location as offer metadata; do not use it
        # as a Backend-side authorization check.
        location_code=proposer_rt.current_location_code,
        status=TradeOfferStatus.PENDING.value,
        expected_seller_asset_version=expected_seller_asset_version,
        expires_world_minute=expires_world_minute,
        created_request_id=request_id,
        created_world_minute=world_minute,
    )

    offer_data = {
        "offer_id": str(offer.offer_id),
        "status": TradeOfferStatus.PENDING.value,
        "side": side,
        "store_code": buying_store.pk if buying_store else None,
        "proposer_npc_id": str(proposer.agent_id),
        "buyer_npc_id": str(buyer.agent_id),
        "seller_npc_id": str(seller.agent_id),
        "item_id": item_id,
        "item_name": _trade_item_name(item_id),
        "amount": amount,
        "gold_amount": gold_amount,
        "expires_world_minute": expires_world_minute,
        "commitment_id": str(commitment.commitment_id) if commitment else None,
    }

    return _ok(
        data=offer_data,
        effects={"offer": offer_data},
        external_delta=0,
        participant_npc_ids=[str(buyer.agent_id), str(seller.agent_id)],
    )


# ── RESPOND_TRADE ────────────────────────────────────────────────────────────


def _do_respond_trade(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    offer_uuid = _parse_uuid(params.get("offer_id"))
    decision = params.get("decision")

    if offer_uuid is None:
        return _fail(ResultCode.TRADE_OFFER_NOT_FOUND)
    if decision not in ("ACCEPT", "REJECT"):
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        try:
            offer = VillageTradeOffer.objects.select_for_update().get(offer_id=offer_uuid)
        except VillageTradeOffer.DoesNotExist:
            return _fail(ResultCode.TRADE_OFFER_NOT_FOUND)

        # Only the non-proposer counterparty may respond.
        participant_ids = {
            str(offer.buyer_npc.agent_id), str(offer.seller_npc.agent_id),
        }
        if str(offer.proposer_npc.agent_id) == str(npc_id) or str(npc_id) not in participant_ids:
            return _fail(ResultCode.TRADE_RESPONSE_FORBIDDEN)

        if offer.status != TradeOfferStatus.PENDING.value:
            return _resolved_offer_failure(offer)

        # DELIVERY commits use the same lock order as the asset settlement:
        # offer -> commitment -> inventories.
        if offer.commitment_id:
            from .phase4_social import social_enabled
            if not social_enabled():
                return _fail("VILLAGE_SOCIAL_DISABLED")
            commitment = NpcCommitment.objects.select_for_update().filter(
                pk=offer.commitment_id,
                status="OPEN",
                commitment_type="DELIVERY",
                promisor_npc=offer.seller_npc,
                beneficiary_npc=offer.buyer_npc,
            ).first()
            if commitment is None:
                return _fail("COMMITMENT_ALREADY_RESOLVED")
            if world_minute >= commitment.due_world_minute:
                return _fail("COMMITMENT_EXPIRED")
            target = commitment.target or {}
            if (
                target.get("item_id") != offer.item_id
                or target.get("amount") != offer.amount
                or target.get("gold_amount") != offer.gold_amount
            ):
                return _fail("COMMITMENT_TARGET_MISMATCH")

        if decision == "REJECT":
            offer.status = TradeOfferStatus.REJECTED.value
            offer.resolved_request_id = request_id
            offer.resolved_world_minute = world_minute
            offer.save(update_fields=[
                "status", "resolved_request_id", "resolved_world_minute", "updated_at",
            ])
            return _ok(
                effects={
                    "offer_id": str(offer.offer_id),
                    "decision": "REJECT",
                    "status": TradeOfferStatus.REJECTED.value,
                },
                external_delta=0,
                # Surface TRADE_TARGET_REJECTED to the buyer's notification while
                # the responder's own action still reports OK (design 6.4).
                event_result_code=ResultCode.TRADE_TARGET_REJECTED,
                participant_npc_ids=[
                    str(offer.buyer_npc.agent_id), str(offer.seller_npc.agent_id),
                ],
                data={
                    "offer_id": str(offer.offer_id),
                    "side": (
                        Side.BUY.value
                        if str(offer.proposer_npc.agent_id) == str(offer.buyer_npc.agent_id)
                        else Side.SELL.value
                    ),
                    "proposer_npc_id": str(offer.proposer_npc.agent_id),
                    "buyer_npc_id": str(offer.buyer_npc.agent_id),
                    "seller_npc_id": str(offer.seller_npc.agent_id),
                    "item_id": offer.item_id,
                    "item_name": _trade_item_name(offer.item_id),
                    "amount": offer.amount,
                    "gold_amount": offer.gold_amount,
                    "decision": "REJECT",
                    "status": TradeOfferStatus.REJECTED.value,
                    "commitment_id": str(offer.commitment_id) if offer.commitment_id else None,
                },
            )

        # ACCEPT
        # Old seller-initiated shop quotations must not bypass fixed valuation.
        # They may still be rejected/cancelled; a new seller action settles now.
        if offer.buying_store_id and offer.proposer_npc_id == offer.seller_npc_id:
            return _fail(ResultCode.TRADE_STORE_FORBIDDEN,
                         data={"failure_kind": "LEGACY_STORE_SELL_OFFER"})
        if world_minute >= offer.expires_world_minute:
            # v1 recommendation (design 11.4): promote the stale PENDING offer
            # to EXPIRED so it can no longer be re-accepted.
            offer.status = TradeOfferStatus.EXPIRED.value
            offer.resolved_request_id = request_id
            offer.resolved_world_minute = world_minute
            offer.save(update_fields=[
                "status", "resolved_request_id", "resolved_world_minute", "updated_at",
            ])
            return _fail(ResultCode.TRADE_OFFER_EXPIRED)

        buyer_id = str(offer.buyer_npc.agent_id)
        seller_id = str(offer.seller_npc.agent_id)

        buying_store = None
        store_account = None
        if offer.buying_store_id:
            buying_store = VillageStore.objects.select_for_update().get(pk=offer.buying_store_id)
            if buying_store.operator_npc_id != offer.buyer_npc_id:
                return _fail(ResultCode.TRADE_STORE_FORBIDDEN)
            if buying_store.status != StoreOperationalStatus.OPEN.value:
                return _fail(ResultCode.TRADE_STORE_CLOSED)
            for participant in (offer.buyer_npc, offer.seller_npc):
                state = NpcRuntimeState.objects.filter(npc=participant).first()
                if state is None or state.health_fp <= 300 or state.action_status in {"AWAY", "SLEEPING"} or (
                    state.away_until_world_minute is not None and state.away_until_world_minute > world_minute
                ) or state.current_action == "SLEEP":
                    return _fail(ResultCode.P3_FORBIDDEN_ACTOR, data={"failure_kind": "TRADE_PARTICIPANT_UNAVAILABLE"})
            store_account = VillageStoreAccount.objects.select_for_update().get(store=buying_store)
        # Lock both inventories in stable order.
        nid_a, nid_b = sorted([buyer_id, seller_id])
        inv_a = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=nid_a)
        inv_b = NpcGlobalInventory.objects.select_for_update().get(npc__agent_id=nid_b)
        buyer_inv = inv_a if nid_a == buyer_id else inv_b
        seller_inv = inv_b if nid_a == buyer_id else inv_a

        item_id = offer.item_id
        amount = offer.amount
        gold_amount = offer.gold_amount

        emergency_supply = False
        if offer.commitment_id and item_id in SAFE_STOCKS:
            buyer_available = sum(
                get_available_slot_amount(slot)
                for slot in NpcGlobalInventorySlot.objects.select_for_update().filter(
                    bag=buyer_inv, item_id=item_id, item_amount__gt=0
                )
                if slot.expires_world_minute is None or slot.expires_world_minute > world_minute
            )
            emergency_supply = (
                buyer_available < SAFE_STOCKS[item_id]
                and gold_amount <= BASE_PRICES[item_id] * amount
            )

        # Re-validate resources under lock.  All checks run before any mutation:
        # buyer money first, then seller items (the consume helper also validates
        # its total before deducting anything).
        available_money = store_account.balance if store_account else _get_buyer_available_money(buyer_inv)
        if available_money < gold_amount:
            return _fail(ResultCode.TRADE_INSUFFICIENT_MONEY, data={"required_amount": gold_amount,
                "available_amount": available_money, "payer": "STORE" if store_account else "PERSONAL"})
        try:
            consumed = _consume_personal_fefo_available(
                seller_inv, item_id, amount, world_minute
            )
        except ValueError:
            return _fail(ResultCode.TRADE_INSUFFICIENT_ITEM)

        # Transfer items (preserve batch attributes) and money.
        for slot, taken in consumed:
            if buying_store:
                VillageStoreInventorySlot.objects.create(
                    store=buying_store, item_id=item_id, item_amount=taken,
                    produced_world_minute=slot.produced_world_minute,
                    expires_world_minute=slot.expires_world_minute,
                    remaining_uses=slot.remaining_uses, source_npc=offer.seller_npc,
                    source_slot_instance_id=slot.instance_id,
                    item_attributes={key: getattr(slot, key) for key in BATCH_ITEM_ATTRIBUTES},
                )
            else:
                add_batch(buyer_inv, item_id, taken, slot.produced_world_minute,
                          slot.expires_world_minute, slot.source_request_id, remaining_uses=slot.remaining_uses,
                          item_attributes={key: getattr(slot, key) for key in BATCH_ITEM_ATTRIBUTES})

        if store_account:
            store_account.balance -= gold_amount
            store_account.account_version += 1
            store_account.save()
            buying_store.inventory_version += 1
            buying_store.save(update_fields=["inventory_version", "updated_at"])
        else:
            buyer_inv.money -= gold_amount
            buyer_inv.asset_version += 1
            buyer_inv.save(update_fields=["money", "asset_version", "updated_at"])

        seller_inv.money += gold_amount
        seller_inv.asset_version += 1
        seller_inv.save(update_fields=["money", "asset_version", "updated_at"])

        offer.status = TradeOfferStatus.ACCEPTED.value
        offer.resolved_request_id = request_id
        offer.resolved_world_minute = world_minute
        offer.save(update_fields=[
            "status", "resolved_request_id", "resolved_world_minute", "updated_at",
        ])

    effects = {
        ("STORE:" + buying_store.pk if buying_store else buyer_id): {"money": -gold_amount, str(item_id): amount},
        seller_id: {"money": gold_amount, str(item_id): -amount},
        "offer_id": str(offer.offer_id),
        "status": TradeOfferStatus.ACCEPTED.value,
    }

    return _ok(
        asset_versions={buyer_id: buyer_inv.asset_version, seller_id: seller_inv.asset_version},
        effects=effects,
        external_delta=0,
        data={
            "offer_id": str(offer.offer_id),
            "side": (
                Side.BUY.value
                if str(offer.proposer_npc.agent_id) == str(offer.buyer_npc.agent_id)
                else Side.SELL.value
            ),
            "proposer_npc_id": str(offer.proposer_npc.agent_id),
            "buyer_npc_id": buyer_id,
            "seller_npc_id": seller_id,
            "status": TradeOfferStatus.ACCEPTED.value,
            "item_id": item_id,
            "item_name": _trade_item_name(item_id),
            "amount": amount,
            "gold_amount": gold_amount,
            "commitment_id": str(offer.commitment_id) if offer.commitment_id else None,
            "emergency_supply": emergency_supply,
            "settlement_completed": True,
            "payment_source": "STORE" if buying_store else "PERSONAL",
            "store_code": buying_store.pk if buying_store else None,
        },
        participant_npc_ids=[buyer_id, seller_id],
    )


# ── CANCEL_TRADE_OFFER ───────────────────────────────────────────────────────


def _do_cancel_trade_offer(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    offer_uuid = _parse_uuid(params.get("offer_id"))
    if offer_uuid is None:
        return _fail(ResultCode.TRADE_OFFER_NOT_FOUND)

    with transaction.atomic():
        try:
            offer = VillageTradeOffer.objects.select_for_update().get(offer_id=offer_uuid)
        except VillageTradeOffer.DoesNotExist:
            return _fail(ResultCode.TRADE_OFFER_NOT_FOUND)

        # Only the original proposer may cancel (it may be buyer or seller).
        if str(offer.proposer_npc.agent_id) != str(npc_id):
            return _fail(ResultCode.TRADE_CANCEL_FORBIDDEN)

        if offer.status != TradeOfferStatus.PENDING.value:
            return _resolved_offer_failure(offer)

        offer.status = TradeOfferStatus.CANCELLED.value
        offer.resolved_request_id = request_id
        offer.resolved_world_minute = world_minute
        offer.save(update_fields=[
            "status", "resolved_request_id", "resolved_world_minute", "updated_at",
        ])

    return _ok(
        data={
            "offer_id": str(offer.offer_id),
            "side": (
                Side.BUY.value
                if str(offer.proposer_npc.agent_id) == str(offer.buyer_npc.agent_id)
                else Side.SELL.value
            ),
            "proposer_npc_id": str(offer.proposer_npc.agent_id),
            "buyer_npc_id": str(offer.buyer_npc.agent_id),
            "seller_npc_id": str(offer.seller_npc.agent_id),
            "item_id": offer.item_id,
            "item_name": _trade_item_name(offer.item_id),
            "amount": offer.amount,
            "gold_amount": offer.gold_amount,
            "status": TradeOfferStatus.CANCELLED.value,
        },
        effects={
            "offer_id": str(offer.offer_id),
            "status": TradeOfferStatus.CANCELLED.value,
        },
        external_delta=0,
        participant_npc_ids=[
            str(offer.buyer_npc.agent_id), str(offer.seller_npc.agent_id),
        ],
    )


# ── LIST_STORE_ITEM ──────────────────────────────────────────────────────────


def _do_list_store_item(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    store_code = params.get("store_code")
    if store_code == "RESTAURANT_STORE":
        from .lucy_restaurant import list_meal
        return list_meal(npc_id, params, world_minute, request_id)
    if params.get("public_meal_code") or params.get("decision_id"):
        return _fail("PUBLIC_MEAL_NOT_AUTHORIZED")
    item_id = params.get("item_id")
    amount = params.get("amount")
    price_mode = params.get("price_mode")
    custom_unit_price = params.get("custom_unit_price")
    expected_operator_asset_version = params.get("expected_operator_asset_version")
    expected_store_inventory_version = params.get("expected_store_inventory_version")

    if not _is_positive_int(item_id):
        return _fail(ResultCode.P3_INVALID_PARAMS)
    if not _is_positive_int(amount):
        return _fail(ResultCode.P3_INVALID_PARAMS)

    # Every real, non-deleted item is eligible. The agent decides whether to
    # list it and at what CUSTOM price; this guard only rejects stale/fake IDs.
    if not Item.objects.filter(item_id=item_id, is_deleted=False).exists():
        return _fail(ResultCode.TRADE_ITEM_NOT_FOR_SALE)

    unit_price = _resolve_final_unit_price(price_mode, custom_unit_price, item_id)
    if unit_price is None:
        return _fail(ResultCode.TRADE_INVALID_CUSTOM_PRICE)

    with transaction.atomic():
        try:
            store = VillageStore.objects.select_for_update().get(store_code=store_code)
        except VillageStore.DoesNotExist:
            return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)

        # Operator check.
        if store.operator_npc is None or str(store.operator_npc.agent_id) != str(npc_id):
            return _fail(ResultCode.TRADE_STORE_FORBIDDEN)

        try:
            operator_inv = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        if (
            expected_operator_asset_version is not None
            and operator_inv.asset_version != expected_operator_asset_version
        ):
            return _fail(ResultCode.TRADE_ASSET_VERSION_CONFLICT)
        if (
            expected_store_inventory_version is not None
            and store.inventory_version != expected_store_inventory_version
        ):
            return _fail(ResultCode.TRADE_STORE_VERSION_CONFLICT)

        # A store may have several ACTIVE listings for the same item at
        # different prices. Restocking at an existing price merges into that
        # listing; a new price gets its own listing. The store row is already
        # locked above, so this selection is serialized with concurrent list
        # operations for the same store.
        active_listings = list(
            VillageStoreListing.objects.select_for_update().filter(
                store=store, item_id=item_id, status=ListingStatus.ACTIVE.value
            ).order_by("listed_world_minute", "listing_id")
        )
        existing = next(
            (listing for listing in active_listings if listing.unit_price == unit_price),
            None,
        )

        stock_source = params.get("stock_source", "PERSONAL")
        if stock_source not in {"PERSONAL", "STORE"}:
            return _fail(ResultCode.P3_INVALID_PARAMS)
        try:
            if stock_source == "STORE":
                from .caravan import consume_unlisted
                consumed = consume_unlisted(store, item_id, amount, world_minute)
            else:
                consumed = _consume_personal_fefo_available(operator_inv, item_id, amount, world_minute)
        except ValueError:
            return _fail(ResultCode.TRADE_INSUFFICIENT_ITEM)

        if existing is None:
            listing = VillageStoreListing.objects.create(
                store=store,
                item_id=item_id,
                price_mode=price_mode,
                unit_price=unit_price,
                status=ListingStatus.ACTIVE.value,
                listed_by_npc=operator_inv.npc,
                created_request_id=request_id,
                listed_world_minute=world_minute,
            )
        else:
            listing = existing

        listing_price_mode = listing.price_mode

        # Write store-owned batches preserving produced/expired/source batch.
        for slot, taken in consumed:
            VillageStoreInventorySlot.objects.create(
                store=store,
                listing=listing,
                item_id=item_id,
                item_amount=taken,
                remaining_uses=slot.remaining_uses,
                produced_world_minute=slot.produced_world_minute,
                expires_world_minute=slot.expires_world_minute,
                source_npc=slot.source_npc if stock_source == "STORE" else operator_inv.npc,
                source_slot_instance_id=slot.source_slot_instance_id if stock_source == "STORE" else slot.instance_id,
                item_attributes=slot.item_attributes if stock_source == "STORE" else
                    {key: getattr(slot, key) for key in BATCH_ITEM_ATTRIBUTES},
            )

        if params.get("stock_source", "PERSONAL") == "PERSONAL":
            operator_inv.asset_version += 1
            operator_inv.save(update_fields=["asset_version", "updated_at"])

        store.inventory_version += 1
        store.save(update_fields=["inventory_version", "updated_at"])

    return _ok(
        asset_versions={str(npc_id): operator_inv.asset_version},
        effects={
            "store_code": store_code,
            "listing_id": str(listing.listing_id),
            "item_id": item_id,
            "amount": amount,
            "unit_price": unit_price,
            "price_mode": listing_price_mode,
            "store_inventory_version": store.inventory_version,
        },
        external_delta=0,
        data={
            "store_code": store_code,
            "listing_id": str(listing.listing_id),
            "item_id": item_id,
            "amount": amount,
            "unit_price": unit_price,
            "price_mode": listing_price_mode,
            "store_inventory_version": store.inventory_version,
        },
    )


# ── UPDATE_STORE_PRICE ───────────────────────────────────────────────────────


def _do_update_store_price(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    listing_uuid = _parse_uuid(params.get("listing_id"))
    price_mode = params.get("price_mode")
    custom_unit_price = params.get("custom_unit_price")
    expected_store_inventory_version = params.get("expected_store_inventory_version")

    if listing_uuid is None:
        return _fail(ResultCode.TRADE_LISTING_NOT_FOUND)

    with transaction.atomic():
        # Resolve store_code from the immutable listing FK, then lock store
        # before listing to honour the stable store→listing lock order.
        try:
            listing_ref = VillageStoreListing.objects.only(
                "listing_id", "store_id"
            ).get(listing_id=listing_uuid)
        except VillageStoreListing.DoesNotExist:
            return _fail(ResultCode.TRADE_LISTING_NOT_FOUND)

        try:
            store = VillageStore.objects.select_for_update().get(
                store_code=listing_ref.store_id
            )
        except VillageStore.DoesNotExist:
            return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)

        try:
            listing = VillageStoreListing.objects.select_for_update().get(
                listing_id=listing_uuid
            )
        except VillageStoreListing.DoesNotExist:
            return _fail(ResultCode.TRADE_LISTING_NOT_FOUND)

        if store.operator_npc is None or str(store.operator_npc.agent_id) != str(npc_id):
            return _fail(ResultCode.TRADE_STORE_FORBIDDEN)

        if listing.status != ListingStatus.ACTIVE.value:
            return _fail(ResultCode.TRADE_LISTING_NOT_ACTIVE)

        if store.store_code == "RESTAURANT_STORE":
            return _fail("RESTAURANT_FIXED_PRICE_REQUIRED")
        unit_price = _resolve_final_unit_price(
            price_mode, custom_unit_price, listing.item_id
        )
        if unit_price is None:
            return _fail(ResultCode.TRADE_INVALID_CUSTOM_PRICE)

        if (
            expected_store_inventory_version is not None
            and store.inventory_version != expected_store_inventory_version
        ):
            return _fail(ResultCode.TRADE_STORE_VERSION_CONFLICT)

        listing.price_mode = price_mode
        listing.unit_price = unit_price
        listing.save(update_fields=["price_mode", "unit_price", "updated_at"])

        store.inventory_version += 1
        store.save(update_fields=["inventory_version", "updated_at"])

    return _ok(
        effects={
            "listing_id": str(listing.listing_id),
            "unit_price": unit_price,
            "price_mode": price_mode,
            "store_inventory_version": store.inventory_version,
        },
        external_delta=0,
        data={
            "listing_id": str(listing.listing_id),
            "unit_price": unit_price,
            "price_mode": price_mode,
            "store_code": store.store_code, "item_id": listing.item_id,
            "store_inventory_version": store.inventory_version,
        },
    )


# ── DELIST_STORE_ITEM ────────────────────────────────────────────────────────


def _do_delist_store_item(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    listing_uuid = _parse_uuid(params.get("listing_id"))
    expected_operator_asset_version = params.get("expected_operator_asset_version")
    expected_store_inventory_version = params.get("expected_store_inventory_version")

    if listing_uuid is None:
        return _fail(ResultCode.TRADE_LISTING_NOT_FOUND)

    with transaction.atomic():
        # Resolve store_code from the immutable listing FK, then lock store
        # before listing to honour the stable store→listing lock order.
        try:
            listing_ref = VillageStoreListing.objects.only(
                "listing_id", "store_id"
            ).get(listing_id=listing_uuid)
        except VillageStoreListing.DoesNotExist:
            return _fail(ResultCode.TRADE_LISTING_NOT_FOUND)

        try:
            store = VillageStore.objects.select_for_update().get(
                store_code=listing_ref.store_id
            )
        except VillageStore.DoesNotExist:
            return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)

        try:
            listing = VillageStoreListing.objects.select_for_update().get(
                listing_id=listing_uuid
            )
        except VillageStoreListing.DoesNotExist:
            return _fail(ResultCode.TRADE_LISTING_NOT_FOUND)

        if store.operator_npc is None or str(store.operator_npc.agent_id) != str(npc_id):
            return _fail(ResultCode.TRADE_STORE_FORBIDDEN)

        if listing.status != ListingStatus.ACTIVE.value:
            return _fail(ResultCode.TRADE_LISTING_NOT_ACTIVE)

        try:
            operator_inv = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        if (
            expected_operator_asset_version is not None
            and operator_inv.asset_version != expected_operator_asset_version
        ):
            return _fail(ResultCode.TRADE_ASSET_VERSION_CONFLICT)
        if (
            expected_store_inventory_version is not None
            and store.inventory_version != expected_store_inventory_version
        ):
            return _fail(ResultCode.TRADE_STORE_VERSION_CONFLICT)

        # Delisting changes visibility; ownership stays with the store.
        returned = 0
        delisted_amount = sum(s.item_amount for s in VillageStoreInventorySlot.objects.filter(store=store, listing=listing))
        VillageStoreInventorySlot.objects.filter(store=store, listing=listing).update(listing=None)

        listing.status = ListingStatus.DELISTED.value
        listing.save(update_fields=["status", "updated_at"])

        store.inventory_version += 1
        store.save(update_fields=["inventory_version", "updated_at"])

    return _ok(
        asset_versions={str(npc_id): operator_inv.asset_version},
        effects={
            "listing_id": str(listing.listing_id),
            "returned_amount": returned,
            "store_inventory_version": store.inventory_version,
        },
        external_delta=0,
        data={
            "listing_id": str(listing.listing_id),
            "returned_amount": returned,
            "delisted_amount": delisted_amount, "store_code": store.store_code, "item_id": listing.item_id,
            "status": listing.status, "stock_destination": "STORE_UNLISTED",
            "store_inventory_version": store.inventory_version,
        },
    )


# ── BUY_FROM_STORE ───────────────────────────────────────────────────────────


def _resolved_offer_failure(offer):
    data = {"offer_id": str(offer.offer_id), "offer_status": offer.status,
            "resolved_world_minute": offer.resolved_world_minute}
    if offer.status == TradeOfferStatus.CANCELLED.value:
        data["cancelled_by_npc_id"] = str(offer.proposer_npc.agent_id)
    return _fail(ResultCode.TRADE_OFFER_ALREADY_RESOLVED, data=data)


def _do_buy_from_store(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    listing_uuid = _parse_uuid(params.get("listing_id"))
    amount = params.get("amount")
    expected_store_inventory_version = params.get("expected_store_inventory_version")
    expected_buyer_asset_version = params.get("expected_buyer_asset_version")
    if listing_uuid and VillageStoreListing.objects.filter(pk=listing_uuid, store_id="RESTAURANT_STORE").exclude(public_meal_code="").exists():
        from .lucy_restaurant import buy_public_meal
        return buy_public_meal(npc_id, params, world_minute, request_id)

    if listing_uuid is None:
        return _fail(ResultCode.TRADE_LISTING_NOT_FOUND)
    if not _is_positive_int(amount):
        return _fail(ResultCode.P3_INVALID_PARAMS)

    with transaction.atomic():
        # Resolve the store code from the (immutable) listing FK, then lock
        # store before listing to honour the stable lock order.
        try:
            listing_ref = VillageStoreListing.objects.only(
                "listing_id", "store_id"
            ).get(listing_id=listing_uuid)
        except VillageStoreListing.DoesNotExist:
            return _fail(ResultCode.TRADE_LISTING_NOT_FOUND)

        try:
            store = VillageStore.objects.select_for_update().get(
                store_code=listing_ref.store_id
            )
        except VillageStore.DoesNotExist:
            return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)

        try:
            listing = VillageStoreListing.objects.select_for_update().get(
                listing_id=listing_uuid
            )
        except VillageStoreListing.DoesNotExist:
            return _fail(ResultCode.TRADE_LISTING_NOT_FOUND)

        if listing.status != ListingStatus.ACTIVE.value:
            return _fail(ResultCode.TRADE_LISTING_NOT_ACTIVE)

        from .phase5_story import get_store_purchase_limit
        remaining_limit = get_store_purchase_limit(npc_id, listing.item_id, world_minute, store.store_code)
        if remaining_limit is not None and amount > remaining_limit:
            return _fail("STORY_RATION_LIMIT_REACHED", data={
                "item_id": listing.item_id,
                "remaining": remaining_limit,
            })

        if store.status not in _TRADABLE_STORE_STATUSES:
            return _fail(ResultCode.TRADE_STORE_CLOSED)

        # Buyer must not be the operator.
        if store.operator_npc is not None and str(store.operator_npc.agent_id) == str(npc_id):
            return _fail(ResultCode.TRADE_STORE_FORBIDDEN)

        if (
            expected_store_inventory_version is not None
            and store.inventory_version != expected_store_inventory_version
        ):
            return _fail(ResultCode.TRADE_STORE_VERSION_CONFLICT)

        try:
            buyer_inv = NpcGlobalInventory.objects.select_for_update().get(
                npc__agent_id=str(npc_id)
            )
        except NpcGlobalInventory.DoesNotExist:
            return _fail(ResultCode.P3_INVALID_PARAMS)

        if (
            expected_buyer_asset_version is not None
            and buyer_inv.asset_version != expected_buyer_asset_version
        ):
            return _fail(ResultCode.TRADE_ASSET_VERSION_CONFLICT)

        # Lock store account + PUBLIC_FUND in a stable order (store code then
        # account code).
        try:
            store_account = VillageStoreAccount.objects.select_for_update().get(store=store)
        except VillageStoreAccount.DoesNotExist:
            return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)

        try:
            public_fund = VillageAccount.objects.select_for_update().get(
                account_code="PUBLIC_FUND"
            )
        except VillageAccount.DoesNotExist:
            return _fail(ResultCode.TRADE_PUBLIC_FUND_NOT_FOUND)

        if store.store_code == "RESTAURANT_STORE":
            from .lucy_cooking import fixed_price
            try:
                for batch in VillageStoreInventorySlot.objects.filter(listing=listing, item_amount__gt=0):
                    if fixed_price(listing.item_id, batch.item_attributes) != listing.unit_price:
                        return _fail("RESTAURANT_PRICE_STALE")
            except ValueError as exc:
                return _fail(str(exc))

        # Fee policy must be legal (rejected transaction, not a skipped fee).
        try:
            fee_amount, store_net_amount = _compute_fee(
                listing.unit_price * amount,
                store.transaction_fee_bps,
                store.minimum_fee,
            )
        except ValueError:
            return _fail(ResultCode.TRADE_INVALID_FEE_CONFIG)

        gross_amount = listing.unit_price * amount

        # Resource re-validation under lock.
        available_money = _get_buyer_available_money(buyer_inv)
        if available_money < gross_amount:
            return _fail(ResultCode.TRADE_INSUFFICIENT_MONEY, data={"required_amount": gross_amount,
                "available_amount": available_money, "payer": "PERSONAL"})
        try:
            consumed = _consume_store_fefo_available(
                store, listing, amount, world_minute
            )
        except ValueError:
            return _fail(ResultCode.TRADE_INSUFFICIENT_ITEM)

        # Transfer items to the buyer (preserve batch attributes).
        purchased_batches = []
        from .phase3_public_economy import _get_eat_whitelist
        edible_items = _get_eat_whitelist()
        for slot, taken in consumed:
            personal_slot = add_batch(
                buyer_inv,
                slot.item_id,
                taken,
                slot.produced_world_minute,
                slot.expires_world_minute,
                request_id,
                remaining_uses=slot.remaining_uses,
                item_attributes=slot.item_attributes,
            )
            batch = {"instance_id": str(personal_slot.instance_id), "item_id": slot.item_id,
                     "amount": taken, "source": "PERSONAL_INVENTORY",
                     "expires_world_minute": slot.expires_world_minute}
            if slot.item_id in edible_items:
                batch["food_instance_id"] = str(personal_slot.instance_id)
            if personal_slot.remaining_uses is not None:
                batch["remaining_uses"] = personal_slot.remaining_uses
            purchased_batches.append(batch)

        # Money: buyer pays gross, store keeps net, fee goes to PUBLIC_FUND.
        buyer_inv.money -= gross_amount
        buyer_inv.asset_version += 1
        buyer_inv.save(update_fields=["money", "asset_version", "updated_at"])

        store_account.balance += store_net_amount
        store_account.account_version += 1
        store_account.save(update_fields=["balance", "account_version", "updated_at"])

        public_fund.balance += fee_amount
        public_fund.asset_version += 1
        public_fund.save(update_fields=["balance", "asset_version", "updated_at"])

        store.inventory_version += 1
        store.save(update_fields=["inventory_version", "updated_at"])

        # Mark SOLD_OUT when no available store inventory remains.
        remaining = sum(
            max(0, s.item_amount - s.reserved_amount)
            for s in VillageStoreInventorySlot.objects.filter(
                store=store, listing=listing, item_amount__gt=0
            )
        )
        if remaining <= 0:
            listing.status = ListingStatus.SOLD_OUT.value
            listing.save(update_fields=["status", "updated_at"])

    item_id = listing.item_id
    effects = {
        str(npc_id): {"money": -gross_amount, str(item_id): amount},
        "STORE:" + store.store_code: {"money": store_net_amount, str(item_id): -amount},
        "PUBLIC_FUND": {"money": fee_amount},
        "listing_id": str(listing.listing_id),
        "gross_amount": gross_amount,
        "fee_amount": fee_amount,
        "store_net_amount": store_net_amount,
    }

    data = {
        "store_code": store.store_code, "item_id": item_id, "unit_price": listing.unit_price,
        "listing_id": str(listing.listing_id),
        "amount": amount,
        "gross_amount": gross_amount,
        "fee_amount": fee_amount,
        "store_net_amount": store_net_amount,
        "store_account_balance": store_account.balance,
        "public_fund_balance": public_fund.balance,
        "store_inventory_version": store.inventory_version,
        "store_account_version": store_account.account_version,
        "public_fund_asset_version": public_fund.asset_version,
        "purchased_batches": purchased_batches,
    }

    return _ok(
        asset_versions={str(npc_id): buyer_inv.asset_version},
        effects=effects,
        external_delta=0,
        data=data,
    )


# ── Read-only store inventory query ──────────────────────────────────────────


def query_store_inventory(
    store_code: str, world_minute: int, viewer_npc_id=None
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Return ``(result_code_or_None, data)`` for the public store inventory.

    Read-only: never writes a receipt, ledger or world event. Location
    authorization is owned by Unity; this endpoint is only a public projection.
    """
    store = VillageStore.objects.filter(store_code=store_code).first()
    if store is None:
        return ResultCode.TRADE_TARGET_NOT_FOUND, None

    if store.status not in _TRADABLE_STORE_STATUSES:
        return ResultCode.TRADE_STORE_CLOSED, None

    items: List[Dict[str, Any]] = []
    active_listings = list(
        VillageStoreListing.objects.filter(
            store=store, status=ListingStatus.ACTIVE.value
        ).order_by("item_id", "unit_price", "listed_world_minute", "listing_id")
    )
    item_ids = {listing.item_id for listing in active_listings}
    # Explicit type evidence lets Agent feedback omit legacy dish defaults on
    # seeds/materials without dropping quality from an unrated, normal dish.
    from recipes.models import Recipe
    from .lucy_catalog import RELIEF_PACK
    dish_item_ids = set(Recipe.objects.filter(item_id__in=item_ids)
                        .exclude(item_id=RELIEF_PACK).values_list("item_id", flat=True))
    names: Dict[int, str] = {}
    if item_ids:
        from items.models import Item

        for item in Item.objects.filter(item_id__in=item_ids):
            names[item.item_id] = item.item_name

    for listing in active_listings:
        available = 0
        batches = []
        from .lucy_restaurant import store_available, public_menu_info
        for slot in VillageStoreInventorySlot.objects.filter(
            store=store, listing=listing, item_amount__gt=0
        ):
            if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
                continue
            available += store_available(slot, world_minute)
            batches.append({**slot.item_attributes, "food_instance_id": str(slot.instance_id),
                            "available_amount": store_available(slot, world_minute),
                            "expires_world_minute": slot.expires_world_minute,
                            "remaining_uses": slot.remaining_uses})
        items.append({
            "listing_id": str(listing.listing_id),
            "item_id": listing.item_id,
            "name": names.get(listing.item_id, str(listing.item_id)),
            "is_dish": listing.item_id in dish_item_ids,
            "available_amount": available,
            "unit_price": listing.unit_price,
            "price_mode": listing.price_mode,
            "batches": batches,
            **public_menu_info(listing, world_minute, viewer_npc_id),
        })

    operator_id = (
        str(store.operator_npc.agent_id) if store.operator_npc is not None else None
    )

    from .models import VillageStoryFact
    from .phase5_story import get_store_purchase_limit, story_enabled
    account = VillageStoreAccount.objects.filter(store=store).first()
    fact = VillageStoryFact.objects.filter(arc_id="BROKEN_TRADE_ROUTE_V1").first() if store_code == "GENERAL_STORE" else None
    public_active = story_enabled() and fact and fact.store_limits_enabled and (fact.ration_limits_until_world_minute is None or world_minute < fact.ration_limits_until_world_minute)
    purchase_limits = {
        "public_daily_limits": fact.ration_daily_limits if public_active else {},
        "store_daily_limits": account.daily_limits if account and account.limits_until_world_minute and world_minute < account.limits_until_world_minute else {},
        "store_expires_world_minute": account.limits_until_world_minute if account else None,
        "public_expires_world_minute": fact.ration_limits_until_world_minute if fact else None,
        "remaining_today": {str(i): get_store_purchase_limit(viewer_npc_id, i, world_minute, store_code) for i in item_ids} if viewer_npc_id else {},
    }
    inventory_status, available_by_item = store_inventory_status(store, world_minute)
    data = {
        "purchase_limits": purchase_limits,
        "store_code": store.store_code,
        "location_code": store.location_code,
        "operator_npc_id": operator_id,
        "operational_status": store.status,
        "inventory_status": inventory_status,
        "store_status": inventory_status,
        "available_by_item": {str(item_id): amount for item_id, amount in available_by_item.items()},
        "inventory_version": store.inventory_version,
        "fee_policy": {
            "transaction_fee_bps": store.transaction_fee_bps,
            "minimum_fee": store.minimum_fee,
            "rounding": "FLOOR",
        },
        "items": items,
    }
    return None, data


# ── Legacy order-system migration ────────────────────────────────────────────
#
# Trigger: intended to be called from a management command or (optionally)
# day-close once ``village_direct_trade_enabled`` is turned on.  It is a
# standalone, idempotent function so it can be exercised directly in tests.


def migrate_legacy_trade_orders(
    request_id: Optional[uuid.UUID] = None, world_minute: Optional[int] = None
) -> Dict[str, Any]:
    """Cancel every non-terminal legacy trade order and release reservations.

    Steps (design section 12): cancel OPEN orders, cancel MATCHED orders and
    ACTIVE deliveries (releasing their implicit item/money reservations), write
    a migration ledger entry + world event, and leave the legacy tables in
    place for historical queries.
    """
    from game_time.models import GlobalGameTimeState

    if world_minute is None:
        ts = GlobalGameTimeState.objects.filter(pk=1).first()
        world_minute = ts.world_minute if ts else 0
    if request_id is None:
        request_id = uuid.uuid4()

    with transaction.atomic():
        open_cancelled = 0
        for order in NpcTradeOrder.objects.filter(status="OPEN").select_for_update():
            order.status = "CANCELLED"
            order.save(update_fields=["status", "updated_at"])
            open_cancelled += 1

        matched_cancelled = 0
        for order in NpcTradeOrder.objects.filter(status="MATCHED").select_for_update():
            order.status = "CANCELLED"
            order.matched_order = None
            order.save(update_fields=["status", "matched_order", "updated_at"])
            matched_cancelled += 1

        delivery_cancelled = 0
        for delivery in NpcTradeDelivery.objects.filter(
            status="ACTIVE"
        ).select_for_update():
            delivery.status = "CANCELLED"
            delivery.save(update_fields=["status", "updated_at"])
            delivery_cancelled += 1

        # Release old reservations: with no ACTIVE deliveries remaining,
        # get_available_slot_amount / get_available_money report full amounts.
        summary = {
            "open_orders_cancelled": open_cancelled,
            "matched_orders_cancelled": matched_cancelled,
            "active_deliveries_cancelled": delivery_cancelled,
        }

        event = VillageWorldEvent.objects.create(
            dedupe_key=f"legacy-trade-migration:{request_id}",
            event_type="LEGACY_TRADE_MIGRATION",
            world_minute=world_minute,
            time_revision=world_minute,
            actor_npc=None,
            participant_npc_ids=[],
            location_code="",
            request_id=request_id,
            effects=summary,
            visibility="SYSTEM",
            public_data=summary,
        )
        EconomicLedgerEntry.objects.create(
            request_id=request_id,
            entry_type="LEGACY_TRADE_MIGRATION",
            actor_npc=None,
            changes=summary,
            external_delta=0,
            world_minute=world_minute,
            event=event,
        )

    return summary
