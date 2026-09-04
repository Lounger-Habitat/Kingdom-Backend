"""Phase 3 direct trade & independent store service.

Implements the 7 Phase 3 direct-trade commands (PROPOSE_TRADE,
RESPOND_TRADE, CANCEL_TRADE_OFFER, LIST_STORE_ITEM, UPDATE_STORE_PRICE,
DELIST_STORE_ITEM, BUY_FROM_STORE) plus the read-only store-inventory query
helper and the legacy order-system migration.

Each handler follows the same signature as the production / public-economy
handlers::

    (npc_id, params, world_minute, request_id) -> dict

Called by ``commit_economy_action`` after contract validation.  All mutations
happen inside the outer ``transaction.atomic`` block, so returning a business
failure leaves the transaction to commit only what the handler explicitly
mutated (the handlers below mutate state only after every validation passes;
the one deliberate exception is marking an expired offer EXPIRED on accept).

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
from items.models import Item

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
    StoreOperationalStatus,
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


def _get_runtime_location(npc_id: int) -> Optional[str]:
    rt = NpcRuntimeState.objects.filter(npc__agent_id=str(npc_id)).first()
    return rt.current_location_code if rt else None


def _get_buyer_available_money(inventory: NpcGlobalInventory) -> int:
    """Buyer-side available money (money minus ACTIVE-delivery reservations)."""
    reserved = (
        NpcTradeDelivery.objects.filter(
            buyer_order__npc=inventory.npc, status="ACTIVE"
        ).aggregate(total=models.Sum("reserved_money"))["total"]
        or 0
    )
    return max(0, inventory.money - reserved)


def _consume_personal_fefo_available(
    inventory: NpcGlobalInventory,
    item_id: int,
    amount: int,
    world_minute: int,
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
    for slot in slots:
        if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
            continue
        total_available += get_available_slot_amount(slot)
    if total_available < amount:
        raise ValueError("INSUFFICIENT_ITEM")

    consumed: List[Tuple[NpcGlobalInventorySlot, int]] = []
    remaining = amount
    for slot in slots:
        if remaining <= 0:
            break
        if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
            continue
        available = get_available_slot_amount(slot)
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
        total_available += max(0, slot.item_amount - slot.reserved_amount)
    if total_available < amount:
        raise ValueError("INSUFFICIENT_ITEM")

    consumed: List[Tuple[VillageStoreInventorySlot, int]] = []
    remaining = amount
    for slot in slots:
        if remaining <= 0:
            break
        if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
            continue
        available = max(0, slot.item_amount - slot.reserved_amount)
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


def _store_inventory_status(store: VillageStore, world_minute: int) -> str:
    """Compute NORMAL / LOW_STOCK / OUT_OF_STOCK from store-owned batches."""
    from .phase3_contract import StoreStatus

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


def _do_propose_trade(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id: uuid.UUID,
) -> Dict[str, Any]:
    target_npc_id = params.get("target_npc_id")
    item_id = params.get("item_id")
    amount = params.get("amount")
    gold_amount = params.get("gold_amount")
    conversation_id = params.get("conversation_id")
    expected_seller_asset_version = params.get("expected_seller_asset_version")
    expires_world_minute = params.get("expires_world_minute")
    commitment_id = _parse_uuid(params.get("commitment_id"))

    if target_npc_id is None or str(target_npc_id) == str(npc_id):
        return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)
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

    seller = NpcDefinition.objects.filter(agent_id=str(target_npc_id)).first()
    if seller is None:
        return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)
    buyer = NpcDefinition.objects.filter(agent_id=str(npc_id)).first()
    if buyer is None:
        return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)

    buyer_rt = NpcRuntimeState.objects.filter(npc=buyer).first()
    seller_rt = NpcRuntimeState.objects.filter(npc=seller).first()
    if buyer_rt is None or seller_rt is None:
        return _fail(ResultCode.TRADE_TARGET_NOT_FOUND)
    if buyer_rt.current_location_code != seller_rt.current_location_code:
        return _fail(ResultCode.TRADE_NOT_COLOCATED)

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

    offer = VillageTradeOffer.objects.create(
        buyer_npc=buyer,
        seller_npc=seller,
        commitment=commitment,
        item_id=item_id,
        amount=amount,
        gold_amount=gold_amount,
        conversation_id=conversation_id,
        location_code=buyer_rt.current_location_code,
        status=TradeOfferStatus.PENDING.value,
        expected_seller_asset_version=expected_seller_asset_version,
        expires_world_minute=expires_world_minute,
        created_request_id=request_id,
        created_world_minute=world_minute,
    )

    offer_data = {
        "offer_id": str(offer.offer_id),
        "status": TradeOfferStatus.PENDING.value,
        "buyer_npc_id": str(buyer.agent_id),
        "seller_npc_id": str(seller.agent_id),
        "item_id": item_id,
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

        # Only the offer's seller may respond.
        if str(offer.seller_npc.agent_id) != str(npc_id):
            return _fail(ResultCode.TRADE_RESPONSE_FORBIDDEN)

        if offer.status != TradeOfferStatus.PENDING.value:
            return _fail(ResultCode.TRADE_OFFER_ALREADY_RESOLVED)

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
                    "buyer_npc_id": str(offer.buyer_npc.agent_id),
                    "decision": "REJECT",
                    "status": TradeOfferStatus.REJECTED.value,
                    "commitment_id": str(offer.commitment_id) if offer.commitment_id else None,
                },
            )

        # ACCEPT
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

        # Re-verify colocation at settlement time.
        buyer_rt = NpcRuntimeState.objects.filter(npc=offer.buyer_npc).first()
        seller_rt = NpcRuntimeState.objects.filter(npc=offer.seller_npc).first()
        if buyer_rt is None or seller_rt is None:
            return _fail(ResultCode.TRADE_NOT_COLOCATED)
        if (
            buyer_rt.current_location_code != offer.location_code
            or seller_rt.current_location_code != offer.location_code
        ):
            return _fail(ResultCode.TRADE_NOT_COLOCATED)

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
        if _get_buyer_available_money(buyer_inv) < gold_amount:
            return _fail(ResultCode.TRADE_INSUFFICIENT_MONEY)
        try:
            consumed = _consume_personal_fefo_available(
                seller_inv, item_id, amount, world_minute
            )
        except ValueError:
            return _fail(ResultCode.TRADE_INSUFFICIENT_ITEM)

        # Transfer items (preserve batch attributes) and money.
        for slot, taken in consumed:
            add_batch(
                buyer_inv,
                item_id,
                taken,
                slot.produced_world_minute or world_minute,
                slot.expires_world_minute,
                request_id,
            )

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
        buyer_id: {"money": -gold_amount, str(item_id): amount},
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
            "buyer_npc_id": buyer_id,
            "status": TradeOfferStatus.ACCEPTED.value,
            "item_id": item_id,
            "amount": amount,
            "gold_amount": gold_amount,
            "commitment_id": str(offer.commitment_id) if offer.commitment_id else None,
            "emergency_supply": emergency_supply,
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

        # Only the original buyer (offer creator) may cancel.
        if str(offer.buyer_npc.agent_id) != str(npc_id):
            return _fail(ResultCode.TRADE_CANCEL_FORBIDDEN)

        if offer.status != TradeOfferStatus.PENDING.value:
            return _fail(ResultCode.TRADE_OFFER_ALREADY_RESOLVED)

        offer.status = TradeOfferStatus.CANCELLED.value
        offer.resolved_request_id = request_id
        offer.resolved_world_minute = world_minute
        offer.save(update_fields=[
            "status", "resolved_request_id", "resolved_world_minute", "updated_at",
        ])

    return _ok(
        data={"offer_id": str(offer.offer_id), "status": TradeOfferStatus.CANCELLED.value},
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

        if _get_runtime_location(npc_id) != store.location_code:
            return _fail(ResultCode.TRADE_WRONG_LOCATION)

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

        # Deduct from operator's personal inventory (preserve batches).
        try:
            consumed = _consume_personal_fefo_available(
                operator_inv, item_id, amount, world_minute
            )
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
                produced_world_minute=slot.produced_world_minute,
                expires_world_minute=slot.expires_world_minute,
                source_npc=operator_inv.npc,
                source_slot_instance_id=slot.instance_id,
            )

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

        if _get_runtime_location(npc_id) != store.location_code:
            return _fail(ResultCode.TRADE_WRONG_LOCATION)

        if listing.status != ListingStatus.ACTIVE.value:
            return _fail(ResultCode.TRADE_LISTING_NOT_ACTIVE)

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

        if _get_runtime_location(npc_id) != store.location_code:
            return _fail(ResultCode.TRADE_WRONG_LOCATION)

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

        # Return remaining (unreserved) store batches to the operator.
        returned = 0
        store_slots = list(
            VillageStoreInventorySlot.objects.select_for_update()
            .filter(store=store, listing=listing, item_amount__gt=0)
            .order_by("instance_id")
        )
        for slot in store_slots:
            take = max(0, slot.item_amount - slot.reserved_amount)
            if take > 0:
                add_batch(
                    operator_inv,
                    slot.item_id,
                    take,
                    slot.produced_world_minute or world_minute,
                    slot.expires_world_minute,
                    request_id,
                )
                returned += take
            # Sold amounts are gone; drop the batch (keep reserved portion if any).
            remaining_reserved = slot.reserved_amount
            if remaining_reserved <= 0:
                slot.delete()
            else:
                slot.item_amount = remaining_reserved
                slot.save(update_fields=["item_amount"])

        listing.status = ListingStatus.DELISTED.value
        listing.save(update_fields=["status", "updated_at"])

        operator_inv.asset_version += 1
        operator_inv.save(update_fields=["asset_version", "updated_at"])

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
            "store_inventory_version": store.inventory_version,
        },
    )


# ── BUY_FROM_STORE ───────────────────────────────────────────────────────────


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
        remaining_limit = get_store_purchase_limit(npc_id, listing.item_id, world_minute)
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

        if _get_runtime_location(npc_id) != store.location_code:
            return _fail(ResultCode.TRADE_WRONG_LOCATION)

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
        if _get_buyer_available_money(buyer_inv) < gross_amount:
            return _fail(ResultCode.TRADE_INSUFFICIENT_MONEY)
        try:
            consumed = _consume_store_fefo_available(
                store, listing, amount, world_minute
            )
        except ValueError:
            return _fail(ResultCode.TRADE_INSUFFICIENT_ITEM)

        # Transfer items to the buyer (preserve batch attributes).
        for slot, taken in consumed:
            add_batch(
                buyer_inv,
                slot.item_id,
                taken,
                slot.produced_world_minute or world_minute,
                slot.expires_world_minute,
                request_id,
            )

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
    }

    return _ok(
        asset_versions={str(npc_id): buyer_inv.asset_version},
        effects=effects,
        external_delta=0,
        data=data,
    )


# ── Read-only store inventory query ──────────────────────────────────────────


def query_store_inventory(
    store_code: str, npc_id: int, world_minute: int
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Return ``(result_code_or_None, data)`` for the public store inventory.

    Read-only: never writes a receipt, ledger or world event.
    """
    store = VillageStore.objects.filter(store_code=store_code).first()
    if store is None:
        return ResultCode.TRADE_TARGET_NOT_FOUND, None

    if store.status not in _TRADABLE_STORE_STATUSES:
        return ResultCode.TRADE_STORE_CLOSED, None

    if _get_runtime_location(npc_id) != store.location_code:
        return ResultCode.TRADE_WRONG_LOCATION, None

    items: List[Dict[str, Any]] = []
    active_listings = list(
        VillageStoreListing.objects.filter(
            store=store, status=ListingStatus.ACTIVE.value
        ).order_by("item_id", "unit_price", "listed_world_minute", "listing_id")
    )
    item_ids = {listing.item_id for listing in active_listings}
    names: Dict[int, str] = {}
    if item_ids:
        from items.models import Item

        for item in Item.objects.filter(item_id__in=item_ids):
            names[item.item_id] = item.item_name

    for listing in active_listings:
        available = 0
        for slot in VillageStoreInventorySlot.objects.filter(
            store=store, listing=listing, item_amount__gt=0
        ):
            if slot.expires_world_minute is not None and slot.expires_world_minute <= world_minute:
                continue
            available += max(0, slot.item_amount - slot.reserved_amount)
        items.append({
            "listing_id": str(listing.listing_id),
            "item_id": listing.item_id,
            "name": names.get(listing.item_id, str(listing.item_id)),
            "available_amount": available,
            "unit_price": listing.unit_price,
            "price_mode": listing.price_mode,
        })

    operator_id = (
        str(store.operator_npc.agent_id) if store.operator_npc is not None else None
    )

    data = {
        "store_code": store.store_code,
        "location_code": store.location_code,
        "operator_npc_id": operator_id,
        "store_status": _store_inventory_status(store, world_minute),
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
