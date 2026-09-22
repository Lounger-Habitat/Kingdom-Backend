from __future__ import annotations

import re

from django.conf import settings
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.authentication import GameJWTAuthentication
from account.env_auth import ENV_SERVER_SCOPE, EnvServerJWTAuthentication
from config.auth import get_player_id

from .models import VillageSimulationConfig
from .phase7_services import (
    operations_snapshot,
    presentation_enabled,
    public_journal,
    public_npc_observation,
    public_overview,
    public_store,
)


def _disabled():
    return Response({"success": False, "resultCode": "VILLAGE_PRESENTATION_DISABLED"}, status=404)


@api_view(["GET"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def caravan_demands(request):
    if not _player_id(request):
        return Response({"success": False, "resultCode": "PLAYER_AUTH_REQUIRED"}, status=403)
    if not presentation_enabled():
        return _disabled()
    values = {}
    if set(request.query_params) - {"day_index", "page"}:
        return Response({"success": False, "resultCode": "INVALID_QUERY_PARAMETER"}, status=400)
    for key in ("day_index", "page"):
        if key not in request.query_params:
            continue
        raw = request.query_params[key]
        if len(request.query_params.getlist(key)) != 1 or not raw.isascii() or not raw.isdigit() or len(raw) > 12 or (key == "page" and int(raw) < 1):
            return Response({"success": False, "resultCode": "INVALID_QUERY_PARAMETER"}, status=400)
        values[key] = int(raw)
    from .demand_presentation import public_demands
    return Response({"success": True, "data": public_demands(**values)})


def _player_id(request):
    if request.auth and request.auth.get("scope") == ENV_SERVER_SCOPE:
        return None
    return get_player_id(request)


def _query(request):
    allowed = {"before_sequence", "limit", "day_index", "category", "npc_id", "location_code"}
    if set(request.query_params) - allowed:
        raise ValueError("unknown query parameter")
    values = {"limit": 50}
    for name in ("before_sequence", "limit", "day_index"):
        if name not in request.query_params:
            continue
        raw = request.query_params[name]
        if not raw.isascii() or not raw.isdigit():
            raise ValueError(name)
        values[name] = int(raw)
    if not 1 <= values["limit"] <= 100:
        raise ValueError("limit")
    if values.get("before_sequence") is not None and values["before_sequence"] < 1:
        raise ValueError("before_sequence")
    for name in ("category", "npc_id", "location_code"):
        if name not in request.query_params:
            continue
        raw = request.query_params[name]
        if not raw or len(raw) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
            raise ValueError(name)
        values[name] = raw.upper() if name != "npc_id" else raw
    return values


@api_view(["GET"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def journal(request):
    player_id = _player_id(request)
    if not player_id:
        return Response({"success": False, "resultCode": "PLAYER_AUTH_REQUIRED"}, status=403)
    if not presentation_enabled():
        return _disabled()
    try:
        filters = _query(request)
    except ValueError:
        return Response({"success": False, "resultCode": "INVALID_QUERY_PARAMETER"}, status=400)
    return Response({"success": True, "data": public_journal(player_id, filters)})


@api_view(["GET"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def overview(request):
    player_id = _player_id(request)
    if not player_id:
        return Response({"success": False, "resultCode": "PLAYER_AUTH_REQUIRED"}, status=403)
    if not presentation_enabled():
        return _disabled()
    if request.query_params:
        return Response({"success": False, "resultCode": "INVALID_QUERY_PARAMETER"}, status=400)
    return Response({"success": True, "data": public_overview(player_id)})


@api_view(["GET"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def store(request, store_code):
    player_id = _player_id(request)
    if not player_id:
        return Response({"success": False, "resultCode": "PLAYER_AUTH_REQUIRED"}, status=403)
    if not presentation_enabled():
        return _disabled()
    if request.query_params:
        return Response({"success": False, "resultCode": "INVALID_QUERY_PARAMETER"}, status=400)
    data = public_store(store_code)
    if data is None:
        return Response({"success": False, "resultCode": "STORE_NOT_FOUND"}, status=404)
    return Response({"success": True, "data": data})


@api_view(["GET"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def market(request):
    if not _player_id(request):
        return Response({"success": False, "resultCode": "PLAYER_AUTH_REQUIRED"}, status=403)
    if not presentation_enabled():
        return _disabled()
    from .market_presentation import public_market
    try:
        data = public_market(request.query_params, viewer=request.user)
    except ValueError:
        return Response({"success": False, "resultCode": "INVALID_QUERY_PARAMETER"}, status=400)
    return Response({"success": True, "data": data})


@api_view(["GET"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def caravan(request, route):
    if not _player_id(request):
        return Response({"success": False, "resultCode": "PLAYER_AUTH_REQUIRED"}, status=403)
    if not presentation_enabled():
        return _disabled()
    if set(request.query_params) - {"history_page"}:
        return Response({"success": False, "resultCode": "INVALID_QUERY_PARAMETER"}, status=400)
    try:
        history_page = int(request.query_params.get("history_page", "1"))
        if history_page < 1:
            raise ValueError
    except (TypeError, ValueError):
        return Response({"success": False, "resultCode": "INVALID_QUERY_PARAMETER"}, status=400)
    from .caravan_presentation import public_caravan
    data = public_caravan(route, history_page=history_page)
    if data is None:
        return Response({"success": False, "resultCode": "CARAVAN_ROUTE_NOT_FOUND"}, status=404)
    return Response({"success": True, "data": data})


@api_view(["GET"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def npc_observation(request, npc_id):
    player_id = _player_id(request)
    if not player_id:
        return Response({"success": False, "resultCode": "PLAYER_AUTH_REQUIRED"}, status=403)
    if not presentation_enabled():
        return _disabled()
    if request.query_params or not re.fullmatch(r"\d{5}", npc_id):
        return Response({"success": False, "resultCode": "INVALID_QUERY_PARAMETER"}, status=400)
    data = public_npc_observation(npc_id)
    if data is None:
        return Response({"success": False, "resultCode": "NPC_NOT_FOUND"}, status=404)
    return Response({"success": True, "data": data})


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def operations(request):
    enabled = VillageSimulationConfig.objects.filter(
        config_key="default", village_debug_overlay_enabled=True
    ).exists()
    if not settings.DEBUG or not enabled:
        return Response({"success": False, "resultCode": "VILLAGE_DEBUG_DISABLED"}, status=404)
    return Response({"success": True, "data": operations_snapshot()})
