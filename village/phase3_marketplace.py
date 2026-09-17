from __future__ import annotations

from typing import Any, Callable, Dict
from django.db.models import QuerySet

from inventory.models import NpcDefinition
from inventory.serializers import serialize_npc_global_bag
from marketplace import services as marketplace_services
from marketplace.serializers import serialize_listing, serialize_transaction

from .phase3_contract import ResultCode


class AgentQueryError(Exception):
    def __init__(self, code, data):
        self.code, self.data = code, data
        super().__init__(code)


def _query_data(result):
    if result.get("result_code") != ResultCode.OK:
        raise AgentQueryError(result.get("result_code", "P3_FEATURE_DISABLED"), result.get("data", {}))
    return result.get("data", {})


def _ok(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "status": "SUCCEEDED",
        "result_code": ResultCode.OK,
        "asset_versions": {},
        "effects": {},
        "data": {},
        "event": {},
        "external_delta": 0,
    }
    base.update(overrides)
    return base


def _fail(result_code: str, **overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "status": "FAILED",
        "result_code": result_code,
        "asset_versions": {},
        "effects": {},
        "data": {},
        "event": {},
        "external_delta": 0,
    }
    base.update(overrides)
    return base


def _get_npc(npc_id: int) -> NpcDefinition:
    return NpcDefinition.objects.get(agent_id=str(npc_id), is_test_only=False)


def _service(name: str) -> Callable[..., Any] | None:
    fn = getattr(marketplace_services, name, None)
    return fn if callable(fn) else None


def _normalize_success(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict) and "result_code" in result:
        payload = dict(result)
        payload.setdefault("status", "SUCCEEDED")
        payload.setdefault("asset_versions", {})
        payload.setdefault("effects", {})
        payload.setdefault("data", {})
        payload.setdefault("event", {})
        payload.setdefault("external_delta", 0)
        return payload
    if isinstance(result, dict):
        return _ok(data=result, effects=result)
    return _ok(data={"value": result})


def _serialize_query_result(name: str, result: Any, npc: NpcDefinition | None = None) -> Dict[str, Any]:
    if name in {"browse_listings", "get_npc_listings"}:
        from django.utils import timezone
        now = timezone.now()
        browsing = name == "browse_listings" and npc is not None
        if browsing:
            from game_time.models import GlobalGameTimeState
            world_minute = GlobalGameTimeState.objects.get(pk=1).world_minute
        listings = []
        for listing in result:
            if browsing:
                if (listing.status != "active" or listing.remaining_amount <= 0
                        or listing.seller_npc_id == npc.pk or listing.price < 0
                        or (listing.expires_at and listing.expires_at <= now)):
                    continue
                # Use the same authoritative freshness interpretation as purchase,
                # including legacy NPC food whose provenance is unverified.
                batch = marketplace_services._listing_batch(listing)
                expiry = batch.get("expiresWorldMinute")
                if batch.get("freshnessUnknown") or (expiry is not None and expiry <= world_minute):
                    continue
            row = serialize_listing(listing, npc)
            if listing.status == "active" and listing.expires_at and listing.expires_at <= now:
                # Reading must not return goods or bump asset versions. Report the
                # expired-but-unsettled state without claiming the refund happened.
                row["can_buy"] = False
                row["expiry_settlement_pending"] = True
            listings.append(row)
        return {"listings": listings, "total": len(listings)}
    if name == "get_npc_history":
        transactions = []
        for tx in result:
            row = serialize_transaction(tx)
            row["role"] = "buyer" if npc is not None and tx.buyer_npc_id == npc.pk else "seller"
            row["buyerNpcId"] = str(tx.buyer_npc.agent_id) if tx.buyer_npc_id else None
            row["sellerNpcId"] = str(tx.seller_npc.agent_id) if tx.seller_npc_id else None
            transactions.append(row)
        return {"transactions": transactions, "total": len(transactions)}
    return {"value": result}


def _extract_pagination(params: Dict[str, Any]) -> tuple[Dict[str, Any], int, int]:
    clean = params.dict() if hasattr(params, "dict") else dict(params)
    try:
        page = int(clean.pop("page", 1) or 1)
        page_size = int(clean.pop("page_size", clean.pop("limit", 20)) or 20)
    except (TypeError, ValueError, OverflowError):
        raise AgentQueryError("INVALID_QUERY_PARAMS", {"allowed_values": {"page": "positive integer", "page_size": "positive integer"}})
    page = max(1, page)
    page_size = max(1, page_size)
    return clean, page, page_size


def _apply_pagination(payload: Dict[str, Any], page: int, page_size: int) -> Dict[str, Any]:
    start = (page - 1) * page_size
    end = start + page_size
    if "listings" in payload:
        total = payload.get("total", len(payload["listings"]))
        return {"listings": payload["listings"][start:end], "total": total, "page": page, "page_size": page_size, "has_more": end < total}
    if "transactions" in payload:
        total = payload.get("total", len(payload["transactions"]))
        return {
            "transactions": payload["transactions"][start:end],
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": end < total,
        }
    return payload


def _serialize_write_result(name: str, result: Any, npc: NpcDefinition) -> Dict[str, Any]:
    if name == "create_npc_listing":
        bag = npc.global_inventory
        bag.refresh_from_db()
        return {
            "listing_id": result.pk,
            "instance_id": str(result.source_instance_id),
            "item_id": result.item_id,
            "item_name": result.item_name_snapshot,
            "amount": result.total_amount,
            "remaining_amount": result.remaining_amount,
            "unit_price": result.price,
            "listing_status": result.status,
            "listing_expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "listing": serialize_listing(result, npc),
            "bag": serialize_npc_global_bag(bag),
            "asset_version": bag.asset_version,
        }
    if name == "buy_listing_for_npc":
        bag = result["bag"]
        bag.refresh_from_db()
        listing = result["listing"]
        tx = result["transaction"]
        payload = {
            "listing_id": listing.pk,
            "item_id": tx.item_id,
            "item_name": tx.item_name_snapshot,
            "purchased_batches": result.get("purchased_batches", []),
            "amount": tx.amount,
            "remaining_amount": listing.remaining_amount,
            "unit_price": tx.unit_price,
            "listing_status": listing.status,
            "transaction_id": tx.pk,
            "total_price": tx.total_price,
            "commission": tx.commission,
            "seller_earnings": tx.seller_earnings,
            "bag": serialize_npc_global_bag(bag),
            "asset_version": bag.asset_version,
            "listing": serialize_listing(result["listing"], npc),
            "transaction": serialize_transaction(result["transaction"]),
        }
        seller_npc = getattr(result["listing"], "seller_npc", None)
        if seller_npc is not None:
            seller_bag = seller_npc.global_inventory
            seller_bag.refresh_from_db()
            payload["seller_npc_id"] = str(seller_npc.agent_id)
            payload["seller_asset_version"] = seller_bag.asset_version
        return payload
    if name == "delist_npc_listing":
        returned_items = result.get("returned_items", []) if isinstance(result, dict) else []
        result = result["bag"] if isinstance(result, dict) else result
        result.refresh_from_db()
        return {
            "bag": serialize_npc_global_bag(result),
            "returned_items": returned_items, "returned_amount": sum(r["amount"] for r in returned_items),
            "asset_version": result.asset_version,
        }
    return {"value": result}


def _call_service(name: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    fn = _service(name)
    if fn is None:
        # ponytail: temporary compatibility shim until marketplace NPC APIs land; replace with direct import once available.
        return _fail(ResultCode.P3_FEATURE_DISABLED, data={"missing_service": name})

    npc = kwargs.pop("viewer_npc", None)
    result = fn(*args, **kwargs)
    if npc is None:
        npc = next((arg for arg in args if isinstance(arg, NpcDefinition)), None)
    if (
        isinstance(result, tuple)
        and len(result) == 2
    ):
        payload, error = result
        if error:
            return _marketplace_failure(name, str(error), args, npc,
                                        payload if isinstance(payload, dict) else {})
        if npc is not None and name in {"create_npc_listing", "buy_listing_for_npc", "delist_npc_listing"}:
            payload = _serialize_write_result(name, payload, npc)
        return _normalize_success(payload)
    if isinstance(result, QuerySet):
        return _ok(data=_serialize_query_result(name, list(result), npc))
    return _normalize_success(result)


def _marketplace_failure(name, message, args, npc, data):
    """Attach recovery facts from the actual result and authorized current state."""
    from marketplace.models import MarketListing
    codes = {
        "金币不足": "MARKETPLACE_INSUFFICIENT_MONEY",
        "数量必须大于0": "MARKETPLACE_INVALID_AMOUNT",
        "价格必须大于0": "MARKETPLACE_INVALID_PRICE",
        "物品保质期来源未核实，暂不能出售": "MARKETPLACE_FRESHNESS_UNKNOWN",
        "挂单物品保质期来源未核实，暂不能成交": "MARKETPLACE_FRESHNESS_UNKNOWN",
        "物品数量不足": "MARKETPLACE_INSUFFICIENT_ITEM",
        "未预留的有效物品数量不足": "MARKETPLACE_INSUFFICIENT_ITEM",
        "购买数量超过剩余数量": "MARKETPLACE_INSUFFICIENT_STOCK",
        "资产版本不匹配": "MARKETPLACE_VERSION_CONFLICT",
        "挂单不存在": "MARKETPLACE_LISTING_NOT_FOUND",
        "挂单不在出售中状态": "MARKETPLACE_LISTING_NOT_ACTIVE",
        "挂单已过期": "MARKETPLACE_LISTING_EXPIRED",
        "挂单物品已过期": "MARKETPLACE_ITEM_EXPIRED",
        "物品已过期": "MARKETPLACE_ITEM_EXPIRED",
        "不能购买自己的挂单": "MARKETPLACE_SELF_PURCHASE",
        "无权操作此挂单": "MARKETPLACE_FORBIDDEN",
        "物品未找到": "MARKETPLACE_ITEM_NOT_FOUND",
        "价格过低，手续费后收入为0": "MARKETPLACE_PRICE_TOO_LOW",
    }
    details = dict(data, failure_reason=message)
    if npc is not None:
        from .phase3_asset_service import get_available_slot_amount
        bag = npc.global_inventory
        bag.refresh_from_db()
        if message == "资产版本不匹配":
            details["current_version"] = bag.asset_version
        if name == "create_npc_listing":
            from uuid import UUID
            try:
                instance_id = UUID(str(args[1]))
            except (ValueError, TypeError, AttributeError):
                instance_id = None
            slot = bag.slots.filter(instance_id=instance_id).first() if instance_id else None
            details["instance_id"] = str(args[1])
            if slot:
                details.update(item_id=slot.item_id, required_amount=args[2],
                               available_amount=get_available_slot_amount(slot))
        elif name in {"buy_listing_for_npc", "delist_npc_listing"}:
            listing = MarketListing.objects.filter(pk=args[1]).first()
            # A failed owner-only delist does not disclose somebody else's data.
            if listing and (name == "buy_listing_for_npc" or listing.seller_npc_id == npc.pk):
                details.update(listing_id=listing.pk, listing_status=listing.status, item_id=listing.item_id)
                if message == "金币不足":
                    details.update(required_amount=listing.price * args[2], available_amount=bag.money, payer=str(npc.agent_id))
                elif message == "购买数量超过剩余数量":
                    details.update(required_amount=args[2], available_amount=listing.remaining_amount)
    return _fail(codes.get(message, message), data=details)


def _do_sell_on_marketplace(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id,
) -> Dict[str, Any]:
    del world_minute
    result = _call_service(
        "create_npc_listing",
        _get_npc(npc_id),
        params.get("instance_id"),
        params.get("amount"),
        params.get("price"),
        expected_asset_version=params.get("expected_asset_version"),
    )
    if result.get("result_code") != ResultCode.OK:
        return result
    payload = result["data"]
    return _ok(
        asset_versions={str(npc_id): payload["asset_version"]},
        effects={"listing": payload["listing"], "bag": payload["bag"]},
        data=payload,
    )


def _do_buy_from_marketplace(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id,
) -> Dict[str, Any]:
    del world_minute
    result = _call_service(
        "buy_listing_for_npc",
        _get_npc(npc_id),
        params.get("listing_id"),
        params.get("amount"),
        expected_asset_version=params.get("expected_buyer_asset_version"),
    )
    if result.get("result_code") != ResultCode.OK:
        return result
    payload = result["data"]
    asset_versions = {str(npc_id): payload["asset_version"]}
    if "seller_npc_id" in payload and "seller_asset_version" in payload:
        asset_versions[payload["seller_npc_id"]] = payload["seller_asset_version"]
    return _ok(
        asset_versions=asset_versions,
        effects={
            "listing": payload["listing"],
            "transaction": payload["transaction"],
            "bag": payload["bag"],
        },
        data=payload,
    )


def _do_delist_from_marketplace(
    npc_id: int,
    params: Dict[str, Any],
    world_minute: int,
    request_id,
) -> Dict[str, Any]:
    del world_minute
    result = _call_service(
        "delist_npc_listing",
        _get_npc(npc_id),
        params.get("listing_id"),
        include_returned_items=True,
        expected_asset_version=params.get("expected_asset_version"),
    )
    if result.get("result_code") != ResultCode.OK:
        return result
    payload = result["data"]
    payload["listing_id"] = int(params["listing_id"])
    payload["listing_status"] = "delisted"
    return _ok(
        asset_versions={str(npc_id): payload["asset_version"]},
        effects={"bag": payload["bag"]},
        data=payload,
    )


def browse_marketplace_query(npc: NpcDefinition, query_params=None) -> dict:
    params, page, page_size = _extract_pagination(query_params or {})
    # Internal option, never supplied by the Agent or added to its tool schema.
    params.pop("expire", None)
    # Browsing is a shopping view, even if a stale client supplies another status.
    params["status"] = "active"
    payload = _query_data(_call_service("browse_listings", viewer_npc=npc, expire=False, **params))
    return _apply_pagination(payload, page, page_size)


def my_marketplace_listings_query(npc: NpcDefinition, query_params=None) -> dict:
    params, page, page_size = _extract_pagination(query_params or {})
    status = params.get("status", "")
    payload = _query_data(_call_service("get_npc_listings", npc, status=status, expire=False))
    return _apply_pagination(payload, page, page_size)


def marketplace_history_query(npc: NpcDefinition, query_params=None) -> dict:
    params, page, page_size = _extract_pagination(query_params or {})
    role = params.get("role", "all")
    payload = _query_data(_call_service("get_npc_history", npc, role=role))
    return _apply_pagination(payload, page, page_size)
