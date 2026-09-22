"""Phase 3 economy HTTP endpoints.

Three endpoints only -- no business logic lives here.
All mutations go through phase3_asset_service / phase3_day_close.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Set

from django.http import JsonResponse
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication
from game_time.models import GlobalGameTimeState
from inventory.models import NpcGlobalInventory, NpcGlobalInventorySlot

from .models import (
    DailyPriceSnapshot,
    NpcRuntimeState,
    NpcTradeDelivery,
    NpcTradeOrder,
    VillageAccount,
    VillageFarmPlot,
    VillageRestaurantState,
    VillageSimulationConfig,
    VillageStore,
    VillageStoreAccount,
    VillageTradeOffer,
)
from .phase3_asset_service import commit_economy_action
from .phase3_direct_trade import _store_inventory_status, query_store_inventory
from .phase3_contract import (
    CROP_CODES,
    PLOT_CODES,
    VILLAGE_ECONOMY_ACTION_V1,
    FarmPlotStatus,
    ResultCode,
    VILLAGE_ECONOMY_RESULT_V1,
)

# Required fields for each endpoint
_ECONOMY_ACTION_REQUIRED_FIELDS = frozenset({
    "schema_version",
    "command_id",
    "request_id",
    "message_id",
    "command_name",
    "npc_id",
    "completed_world_minute",
})

_DAY_CLOSE_REQUIRED_FIELDS = frozenset({
    "day_index",
    "request_id",
})
from .phase3_day_close import get_restaurant_status, get_store_status, handle_day_close


# ── Allowed top-level fields ──────────────────────────────────────────────────

_ECONOMY_ACTION_ALLOWED_FIELDS: Set[str] = {
    "schema_version",
    "command_id",
    "request_id",
    "message_id",
    "command_name",
    "npc_id",
    "completed_world_minute",
    "time_revision",
    "current_location_code",
    "params",
}

_DAY_CLOSE_ALLOWED_FIELDS: Set[str] = {
    "day_index",
    "request_id",
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _validate_uuid_field(value: Any, field_name: str) -> bool:
    """Return True if value is a valid UUID string."""
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _validate_int_field(value: Any) -> bool:
    """Return True if value is an int (not bool)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _payload_hash(payload: Dict[str, Any]) -> str:
    """Stable SHA-256 hash for idempotency comparison."""
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _error_response(
    result_code: str, status: int = 400, **extra: Any
) -> Response:
    """Build a standard error response."""
    body: Dict[str, Any] = {
        "success": False,
        "result_code": result_code,
    }
    body.update(extra)
    return Response(body, status=status)


def _success_response(result: Dict[str, Any]) -> Response:
    """Map business result to HTTP status."""
    result_code = result.get("result_code", ResultCode.OK)
    if result_code in (ResultCode.P3_REQUEST_CONFLICT, ResultCode.TRADE_REQUEST_CONFLICT):
        return Response(result, status=409)
    return Response(result, status=200)


# ── POST /api/village/economy/actions/complete ────────────────────────────────


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def economy_actions_complete(request):
    """All Phase 3 actions, and EAT/COMMUNITY_MEAL when economy enabled."""
    try:
        data = request.data if isinstance(request.data, dict) else None
        if data is None:
            return _error_response(ResultCode.P3_INVALID_PARAMS)

        # Reject unknown top-level fields
        unknown = set(data.keys()) - _ECONOMY_ACTION_ALLOWED_FIELDS
        if unknown:
            return _error_response(
                ResultCode.P3_INVALID_PARAMS,
                detail=f"Unknown fields: {', '.join(sorted(unknown))}",
            )

        # Validate required fields
        for field in _ECONOMY_ACTION_REQUIRED_FIELDS:
            if field not in data:
                return _error_response(
                    ResultCode.P3_INVALID_PARAMS,
                    detail=f"Missing required field: {field}",
                )

        # Validate UUIDs
        for uuid_field in ("command_id", "request_id", "message_id"):
            if not _validate_uuid_field(data.get(uuid_field), uuid_field):
                return _error_response(
                    ResultCode.P3_INVALID_PARAMS,
                    detail=f"Invalid UUID: {uuid_field}",
                )

        # Validate integer fields
        for int_field in ("npc_id", "completed_world_minute"):
            if not _validate_int_field(data.get(int_field)):
                return _error_response(
                    ResultCode.P3_INVALID_PARAMS,
                    detail=f"Invalid integer: {int_field}",
                )

        # Validate schema_version
        if data.get("schema_version") != VILLAGE_ECONOMY_ACTION_V1:
            return _error_response(
                ResultCode.P3_INVALID_PARAMS,
                detail="Invalid schema_version",
            )

        # Validate params is dict
        if not isinstance(data.get("params"), dict):
            return _error_response(
                ResultCode.P3_INVALID_PARAMS,
                detail="params must be a dict",
            )

        # Call the unified asset service
        result = commit_economy_action(data)
        return _success_response(result)

    except Exception:
        return Response(
            {
                "success": False,
                "result_code": ResultCode.P3_INVALID_PARAMS,
                "detail": "Internal server error",
            },
            status=500,
        )


# ── GET /api/village/economy/stores/<store_code>/inventory ────────────────────


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def economy_store_inventory(request, store_code):
    """Read-only public store inventory query (INSPECT_STORE_INVENTORY).

    Never writes a receipt, ledger or world event. Unity performs the NPC
    location check before requesting this public projection.
    """
    try:
        time_state = GlobalGameTimeState.objects.filter(pk=1).first()
        world_minute = time_state.world_minute if time_state else 0

        result_code, data = query_store_inventory(store_code, world_minute, request.query_params.get("npc_id"))
        if result_code is not None:
            return _error_response(result_code, status=200)
        return Response(
            {"success": True, "result_code": ResultCode.OK, "data": data},
            status=200,
        )
    except Exception:
        return _error_response(ResultCode.P3_INVALID_PARAMS, status=500)


# ── GET /api/village/economy/snapshot ─────────────────────────────────────────


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def economy_snapshot(request):
    """Unity startup/reconnect/presentation recovery. Read-only, no mutations."""
    try:
        # Current world time
        time_state = GlobalGameTimeState.objects.filter(pk=1).first()
        world_minute = time_state.world_minute if time_state else 0

        # Last closed day index
        account = VillageAccount.objects.filter(account_code="PUBLIC_FUND").first()
        last_closed_day_index = account.last_closed_day_index if account else -1
        public_fund_balance = account.balance if account else 0

        # Read-only projection also fails closed if a lease awaits tick cleanup.
        from .player_farm import project_plot
        farm_plots = [project_plot(plot, world_minute) for plot in
            VillageFarmPlot.objects.select_related("lessee_player").filter(
                plot_code__in=PLOT_CODES).order_by("plot_code")]

        # Prices: latest published snapshot, fallback to base prices
        from .phase3_catalog import BASE_PRICES, farm_crop_catalog
        from .farm_irrigation import project_irrigation
        irrigation = project_irrigation()

        latest_published = (
            DailyPriceSnapshot.objects.filter(status="PUBLISHED")
            .order_by("-day_index")
            .first()
        )
        if latest_published:
            prices = {
                str(k): v for k, v in (latest_published.prices or {}).items()
            }
        else:
            prices = {str(k): v for k, v in BASE_PRICES.items()}

        # Restaurant status
        current_day = world_minute // 1440
        rest_state = (
            VillageRestaurantState.objects.order_by("-day_index").first()
        )
        restaurant_status = rest_state.status if rest_state else "OPEN"

        # Store status
        store_status = get_store_status(world_minute)

        # Open orders
        open_orders = list(
            NpcTradeOrder.objects.filter(status="OPEN")
            .order_by("created_at")
            .values(
                "order_id",
                "side",
                "npc_id",
                "item_id",
                "amount",
                "unit_price",
                "delivery_point_code",
                "expires_world_minute",
            )[:20]
        )
        # Convert UUID to string
        for order in open_orders:
            order["order_id"] = str(order["order_id"])

        # Active deliveries
        active_deliveries = list(
            NpcTradeDelivery.objects.filter(status="ACTIVE")
            .order_by("created_at")
            .values(
                "delivery_id",
                "buyer_order_id",
                "seller_order_id",
                "delivery_point_code",
                "reserved_amount",
                "reserved_money",
                "expires_world_minute",
            )[:20]
        )
        for delivery in active_deliveries:
            delivery["delivery_id"] = str(delivery["delivery_id"])

        # NPC asset versions
        from inventory.models import NpcGlobalInventory

        npc_asset_versions = {}
        for inv in NpcGlobalInventory.objects.filter(
            npc__agent_id__in=["10011", "10012", "10013", "10014", "10015"]
        ):
            npc_asset_versions[inv.npc.agent_id] = inv.asset_version

        # Stores: independent status + inventory/account versions.  Account
        # balance is exposed only in the ``audit`` sub-key (v1 decision: keep
        # the read-only snapshot non-operator-neutral by tagging it audit-only).
        stores = []
        for store in VillageStore.objects.order_by("store_code"):
            store_account = VillageStoreAccount.objects.filter(store=store).first()
            inventory_status = _store_inventory_status(store, world_minute)
            stores.append({
                "store_code": store.store_code,
                "location_code": store.location_code,
                "operator_npc_id": (
                    str(store.operator_npc.agent_id) if store.operator_npc else None
                ),
                "operational_status": store.status,
                "inventory_status": inventory_status,
                "store_status": inventory_status,
                "inventory_version": store.inventory_version,
                "account_version": store_account.account_version if store_account else 0,
                "audit": {
                    "store_account_balance": (
                        store_account.balance if store_account else 0
                    ),
                },
            })

        # Pending trade-offer summary (sanitised: no conversation_id).
        pending_offers = list(
            VillageTradeOffer.objects.filter(status="PENDING")
            .order_by("created_at")
            .values(
                "offer_id",
                "buyer_npc__agent_id",
                "seller_npc__agent_id",
                "proposer_npc__agent_id", "buying_store_id", "location_code",
                "item_id",
                "amount",
                "gold_amount",
                "expires_world_minute",
            )
        )
        pending_trade_offers = {
            "count": len(pending_offers),
            "offers": [
                {
                    "offer_id": str(o["offer_id"]),
                    "buyer_npc_id": str(o["buyer_npc__agent_id"]),
                    "seller_npc_id": str(o["seller_npc__agent_id"]),
                    "proposer_npc_id": str(o["proposer_npc__agent_id"]),
                    "side": "BUY" if o["proposer_npc__agent_id"] == o["buyer_npc__agent_id"] else "SELL",
                    "store_code": o["buying_store_id"],
                    "location_code": o["location_code"],
                    "item_id": o["item_id"],
                    "amount": o["amount"],
                    "gold_amount": o["gold_amount"],
                    "expires_world_minute": o["expires_world_minute"],
                }
                for o in pending_offers
            ],
        }

        from .caravan import projection
        return Response(
            {
                "success": True,
                "data": {
                    "caravan": projection(world_minute),
                    "world_minute": world_minute,
                    "last_closed_day_index": last_closed_day_index,
                    "allowed_plot_codes": list(PLOT_CODES),
                    "allowed_crop_codes": list(CROP_CODES),
                    "crop_catalog": farm_crop_catalog(irrigation),
                    "farm_irrigation": irrigation,
                    "farm_plots": farm_plots,
                    "prices": prices,
                    "restaurant_status": restaurant_status,
                    "operational_status": (
                        VillageStore.objects.filter(store_code="GENERAL_STORE")
                        .values_list("status", flat=True).first()
                    ),
                    "inventory_status": store_status,
                    "store_status": store_status,
                    "open_orders": open_orders,
                    "active_deliveries": active_deliveries,
                    "public_fund_balance": public_fund_balance,
                    "public_fund_asset_version": account.asset_version if account else 0,
                    "npc_asset_versions": npc_asset_versions,
                    "stores": stores,
                    "pending_trade_offers": pending_trade_offers,
                },
            }
        )

    except Exception:
        return Response(
            {
                "success": False,
                "result_code": ResultCode.P3_INVALID_PARAMS,
                "detail": "Internal server error",
            },
            status=500,
        )


# ── POST /api/village/economy/day-close ──────────────────────────────────────


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def economy_day_close(request):
    """Day-close: spoilage, traveler sales, price draft, etc."""
    try:
        data = request.data if isinstance(request.data, dict) else None
        if data is None:
            return _error_response(ResultCode.P3_INVALID_PARAMS)

        # Reject unknown top-level fields
        unknown = set(data.keys()) - _DAY_CLOSE_ALLOWED_FIELDS
        if unknown:
            return _error_response(
                ResultCode.P3_INVALID_PARAMS,
                detail=f"Unknown fields: {', '.join(sorted(unknown))}",
            )

        # Validate required fields
        for field in _DAY_CLOSE_REQUIRED_FIELDS:
            if field not in data:
                return _error_response(
                    ResultCode.P3_INVALID_PARAMS,
                    detail=f"Missing required field: {field}",
                )

        # Validate day_index
        day_index = data.get("day_index")
        if not _validate_int_field(day_index) or day_index < 0:
            return _error_response(
                ResultCode.P3_INVALID_PARAMS,
                detail="Invalid day_index",
            )

        # Validate request_id
        if not _validate_uuid_field(data.get("request_id"), "request_id"):
            return _error_response(
                ResultCode.P3_INVALID_PARAMS,
                detail="Invalid UUID: request_id",
            )

        request_id = uuid.UUID(str(data["request_id"]))

        # Get current world minute
        time_state = GlobalGameTimeState.objects.filter(pk=1).first()
        world_minute = time_state.world_minute if time_state else 0

        result = handle_day_close(data, world_minute, request_id)
        return _success_response(result)

    except Exception:
        return Response(
            {
                "success": False,
                "result_code": ResultCode.P3_INVALID_PARAMS,
                "detail": "Internal server error",
            },
            status=500,
        )
