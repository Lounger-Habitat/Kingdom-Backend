from __future__ import annotations

from typing import Any, Callable, Dict
from django.db.models import QuerySet

from inventory.models import NpcDefinition
from inventory.serializers import serialize_npc_global_bag
from marketplace import services as marketplace_services
from marketplace.serializers import serialize_listing, serialize_transaction

from .phase3_contract import ResultCode


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
    if name == "browse_listings":
        listings = [serialize_listing(listing, npc) for listing in result]
        return {"listings": listings, "total": len(listings)}
    if name == "get_npc_listings":
        listings = [serialize_listing(listing, npc) for listing in result]
        return {"listings": listings, "total": len(listings)}
    if name == "get_npc_history":
        transactions = [serialize_transaction(tx) for tx in result]
        return {"transactions": transactions, "total": len(transactions)}
    return {"value": result}


def _extract_pagination(params: Dict[str, Any]) -> tuple[Dict[str, Any], int, int]:
    clean = params.dict() if hasattr(params, "dict") else dict(params)
    page = int(clean.pop("page", 1) or 1)
    page_size = int(clean.pop("page_size", clean.pop("limit", 20)) or 20)
    page = max(1, page)
    page_size = max(1, page_size)
    return clean, page, page_size


def _apply_pagination(payload: Dict[str, Any], page: int, page_size: int) -> Dict[str, Any]:
    start = (page - 1) * page_size
    end = start + page_size
    if "listings" in payload:
        total = payload.get("total", len(payload["listings"]))
        return {"listings": payload["listings"][start:end], "total": total, "page": page, "page_size": page_size}
    if "transactions" in payload:
        total = payload.get("total", len(payload["transactions"]))
        return {
            "transactions": payload["transactions"][start:end],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    return payload


def _serialize_write_result(name: str, result: Any, npc: NpcDefinition) -> Dict[str, Any]:
    if name == "create_npc_listing":
        bag = npc.global_inventory
        bag.refresh_from_db()
        return {
            "listing": serialize_listing(result, npc),
            "bag": serialize_npc_global_bag(bag),
            "asset_version": bag.asset_version,
        }
    if name == "buy_listing_for_npc":
        bag = result["bag"]
        bag.refresh_from_db()
        payload = {
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
        result.refresh_from_db()
        return {
            "bag": serialize_npc_global_bag(result),
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
            return _fail(str(error), data=payload if isinstance(payload, dict) else {})
        if npc is not None and name in {"create_npc_listing", "buy_listing_for_npc", "delist_npc_listing"}:
            payload = _serialize_write_result(name, payload, npc)
        return _normalize_success(payload)
    if isinstance(result, QuerySet):
        return _ok(data=_serialize_query_result(name, list(result), npc))
    return _normalize_success(result)


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
        expected_asset_version=params.get("expected_asset_version"),
    )
    if result.get("result_code") != ResultCode.OK:
        return result
    payload = result["data"]
    return _ok(
        asset_versions={str(npc_id): payload["asset_version"]},
        effects={"bag": payload["bag"]},
        data=payload,
    )


def browse_marketplace_query(npc: NpcDefinition, query_params=None) -> dict:
    params, page, page_size = _extract_pagination(query_params or {})
    payload = _call_service("browse_listings", viewer_npc=npc, **params).get("data", {})
    return _apply_pagination(payload, page, page_size)


def my_marketplace_listings_query(npc: NpcDefinition, query_params=None) -> dict:
    params, page, page_size = _extract_pagination(query_params or {})
    status = params.get("status", "")
    payload = _call_service("get_npc_listings", npc, status=status).get("data", {})
    return _apply_pagination(payload, page, page_size)


def marketplace_history_query(npc: NpcDefinition, query_params=None) -> dict:
    params, page, page_size = _extract_pagination(query_params or {})
    role = params.get("role", "all")
    payload = _call_service("get_npc_history", npc, role=role).get("data", {})
    return _apply_pagination(payload, page, page_size)
