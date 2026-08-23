import uuid

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication

from .models import NpcProfile, VillageLocationDefinition, VillageSimulationConfig
from .runtime_services import (
    ActionService,
    BootstrapService,
    CheckpointService,
    CommandService,
    EventService,
    OutboxService,
    ResultCode,
    TimeService,
)
from .phase2_services import LifeActionService, NeedsService, runtime_summary
from .services import validate_bootstrap
from .trusted_persistence import save_snapshots


def _config_data(config):
    return {
        "configKey": config.config_key,
        "tickIntervalSeconds": config.tick_interval_seconds,
        "hubCommandTimeoutSeconds": config.hub_command_timeout_seconds,
        "commandQueueLimit": config.command_queue_limit,
        "dayStartMinute": config.day_start_minute,
        "dayCloseMinute": config.day_close_minute,
        "maxCatchupDays": config.max_catchup_days,
        "featureFlags": {
            "villageSimulationEnabled": config.village_simulation_enabled,
            "villageNeedsEnabled": config.village_needs_enabled,
            "villageEconomyEnabled": config.village_economy_enabled,
            "villageSocialEnabled": config.village_social_enabled,
            "villageStoryEnabled": config.village_story_enabled,
            "villageHubCommandAdapterEnabled": config.village_hub_command_adapter_enabled,
            "villageOfflineCatchupEnabled": config.village_offline_catchup_enabled,
            "villageDebugOverlayEnabled": config.village_debug_overlay_enabled,
        },
        "debugLogsEnabled": config.debug_logs_enabled,
    }


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def village_config(request):
    config = VillageSimulationConfig.objects.filter(config_key="default").first()
    if config is None:
        return Response(
            {
                "success": False,
                "message": "村庄配置尚未初始化",
                "resultCode": "VILLAGE_NPC_NOT_BOOTSTRAPPED",
            },
            status=409,
        )
    return Response({"success": True, "data": _config_data(config)})


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def village_npcs(request):
    rows = []
    for profile in NpcProfile.objects.select_related("npc").filter(is_active=True):
        rows.append(
            {
                "agentId": profile.npc.agent_id,
                "name": profile.npc.name,
                "displayName": profile.display_name,
                "occupation": profile.occupation,
                "homeLocationCode": profile.home_location_code,
                "workLocationCode": profile.work_location_code,
                "socialLocationCode": profile.social_location_code,
                "personalityTraits": profile.personality_traits,
                "personalGoal": profile.personal_goal,
                "capabilities": profile.capabilities,
                "needRates": profile.need_rates,
            }
        )
    return Response({"success": True, "data": rows})


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def village_locations(request):
    rows = []
    for location in VillageLocationDefinition.objects.filter(is_active=True):
        rows.append(
            {
                "locationCode": location.location_code,
                "displayName": location.display_name,
                "sceneName": location.scene_name,
                "centerAnchorCode": location.center_anchor_code,
                "locationType": location.location_type,
                "bounds": location.bounds,
                "locationTags": location.location_tags,
                "openingHours": location.opening_hours,
                "capacity": location.capacity,
                "supportedActionTypes": location.supported_action_types,
                "allowedOccupations": location.allowed_occupations,
                "requiredStoryStates": location.required_story_states,
                "isPublic": location.is_public,
            }
        )
    return Response({"success": True, "data": rows})


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def bootstrap_validation(request):
    result = validate_bootstrap()
    return Response({"success": result["valid"], "data": result}, status=200 if result["valid"] else 409)


# ============================================================================
# 阶段一：Runtime API
# ============================================================================

@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def runtime_bootstrap(request):
    """GET /api/village/runtime/bootstrap"""
    data = BootstrapService.get_bootstrap()
    if "error" in data:
        return Response({"success": False, "resultCode": data["error"]}, status=409)
    return Response({"success": True, "data": data})


@api_view(["PUT"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def runtime_checkpoint(request):
    """Persist a trusted Unity snapshot using last-write-wins semantics."""
    result = save_snapshots(dict(request.data) if isinstance(request.data, dict) else {})
    status = 200 if result["success"] else 400
    return Response({
        "success": result["success"],
        "resultCode": result["result_code"],
        "results": result.get("results", []),
    }, status=status)


@api_view(["PUT"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def runtime_time(request):
    """PUT /api/village/runtime/time"""
    world_minute = request.data.get("world_minute")
    expected_time_revision = request.data.get("expected_time_revision")
    request_id = request.data.get("request_id")

    if world_minute is None or expected_time_revision is None:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "缺少 world_minute 或 expected_time_revision"},
            status=400,
        )

    try:
        request_id_uuid = uuid.UUID(request_id) if request_id else None
    except ValueError:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "无效的 request_id"},
            status=400,
        )

    success, result_code, receipt = TimeService.advance_time(
        world_minute=world_minute,
        expected_time_revision=expected_time_revision,
        request_id=request_id_uuid,
    )

    if not success:
        return Response({"success": False, "resultCode": result_code}, status=409)

    return Response({
        "success": True,
        "resultCode": result_code,
        "data": {
            "world_minute": world_minute,
            "time_revision": receipt.time_revision if receipt else None,
            "request_id": str(receipt.request_id) if receipt else None,
        },
    })


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def actions_complete(request):
    """POST /api/village/actions/complete"""
    command_id = request.data.get("command_id")
    request_id = request.data.get("request_id")
    success = bool(request.data.get("success"))
    result_code = request.data.get("result_code") or ResultCode.INVALID_COMMAND_SCHEMA
    message = request.data.get("message", "")
    data = request.data.get("data") if isinstance(request.data.get("data"), dict) else {}
    if not command_id or not request_id:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "缺少 command_id 或 request_id"},
            status=400,
        )

    try:
        command_id_uuid = uuid.UUID(command_id)
        request_id_uuid = uuid.UUID(request_id)
    except ValueError:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "无效的 ID"},
            status=400,
        )

    ok, result_code, receipt = ActionService.complete_action(
        command_id=command_id_uuid,
        request_id=request_id_uuid,
        success=success,
        result_code=result_code,
        message=message,
        data=data,
    )

    if not ok:
        return Response({"success": False, "resultCode": result_code}, status=409)

    return Response({
        "success": True,
        "resultCode": result_code,
        "data": {
            "request_id": str(receipt.request_id) if receipt else None,
            "world_minute": receipt.world_minute if receipt else None,
            "time_revision": receipt.time_revision if receipt else None,
            "state_version": receipt.state_version if receipt else None,
        },
    })


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def runtime_advance_needs(request):
    """POST /api/village/runtime/advance-needs; no needs values are accepted."""
    if not isinstance(request.data, dict):
        return Response({"success": False, "resultCode": ResultCode.INVALID_COMMAND_SCHEMA}, status=400)
    result = NeedsService.advance(dict(request.data))
    status = 200 if result["success"] else 409
    data = {key: value for key, value in result.items() if key not in {"receipt", "success", "result_code"}}
    return Response({"success": result["success"], "resultCode": result["result_code"], "data": data}, status=status)


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def life_actions_execute(request):
    """POST /api/village/life-actions/execute; Backend owns completion facts."""
    if not isinstance(request.data, dict):
        return Response({"success": False, "resultCode": ResultCode.INVALID_COMMAND_SCHEMA}, status=400)
    result = LifeActionService.execute(dict(request.data))
    status = 200 if result["success"] else 409
    data = {key: value for key, value in result.items() if key not in {"receipt", "world_event", "success", "result_code"}}
    return Response({"success": result["success"], "resultCode": result["result_code"], "data": data}, status=status)


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def runtime_summary_view(request):
    npc_id = request.query_params.get("npc_id", "")
    result = runtime_summary(npc_id)
    status = 200 if result["success"] else 409
    data = {key: value for key, value in result.items() if key not in {"success", "result_code"}}
    return Response({"success": result["success"], "resultCode": result["result_code"], "data": data}, status=status)


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def npc_runtime_summary(request, npc_id):
    """Frozen P4 route: GET /api/village/npcs/{npc_id}/runtime-summary."""
    result = runtime_summary(npc_id)
    status = 200 if result["success"] else 409
    data = {key: value for key, value in result.items() if key not in {"success", "result_code"}}
    return Response({"success": result["success"], "resultCode": result["result_code"], "data": data}, status=status)


def _discover_parameter_schema(action):
    fields = {
        "MOVE": {"location_code": {"type": "string"}, "point_code": {"type": "string"}},
        "WAIT": {"duration_minutes": {"type": "integer", "minimum": 1}},
        "GO_HOME": {},
        "EAT": {"source": {"type": "string"}, "food_instance_id": {"type": "string"}, "expected_asset_version": {"type": "integer", "minimum": 0}},
        "REST": {}, "SLEEP": {}, "WORK_PLACEHOLDER": {},
        "SOCIALIZE": {"target_npc_id": {"type": "string"}, "reservation_id": {"type": "string", "format": "uuid"}},
        "PLANT": {"plot_code": {"type": "string"}, "crop_code": {"type": "string"}, "expected_asset_version": {"type": "integer", "minimum": 0}},
        "HARVEST": {"plot_code": {"type": "string"}, "expected_asset_version": {"type": "integer", "minimum": 0}},
        "HUNT": {"hunt_code": {"type": "string"}}, "COOK": {"recipe_id": {"type": "integer"}},
        "CREATE_ORDER": {"side": {"type": "string"}, "item_id": {"type": "integer"}, "quantity": {"type": "integer", "minimum": 1}, "limit_price": {"type": "integer", "minimum": 0}},
        "CANCEL_ORDER": {"order_id": {"type": "string", "format": "uuid"}}, "START_DELIVERY": {"order_id": {"type": "string", "format": "uuid"}},
        "IMPORT_GOODS": {"item_id": {"type": "integer"}, "amount": {"type": "integer", "minimum": 1}},
        "EXPORT_GOODS": {"item_id": {"type": "integer"}, "amount": {"type": "integer", "minimum": 1}},
        "RATE_DISH": {"recipe_id": {"type": "integer"}, "rating": {"type": "integer", "minimum": 1, "maximum": 5}},
        "PUBLISH_PRICE_INDEX": {"prices": {"type": "object"}}, "AUDIT_PUBLIC_FUND": {},
        "PROPOSE_TRADE": {"target_npc_id": {"type": "string"}, "item_id": {"type": "integer"}, "quantity": {"type": "integer", "minimum": 1}},
        "RESPOND_TRADE": {"offer_id": {"type": "string", "format": "uuid"}, "response": {"type": "string"}},
        "CANCEL_TRADE_OFFER": {"offer_id": {"type": "string", "format": "uuid"}},
        "LIST_STORE_ITEM": {"item_id": {"type": "integer"}, "quantity": {"type": "integer", "minimum": 1}, "price": {"type": "integer", "minimum": 0}},
        "UPDATE_STORE_PRICE": {"listing_id": {"type": "string", "format": "uuid"}, "price": {"type": "integer", "minimum": 0}},
        "DELIST_STORE_ITEM": {"listing_id": {"type": "string", "format": "uuid"}},
        "BUY_FROM_STORE": {"listing_id": {"type": "string", "format": "uuid"}, "quantity": {"type": "integer", "minimum": 1}},
        "INSPECT_STORE_INVENTORY": {"store_code": {"type": "string"}}, "INSPECT_SOCIAL_STATE": {},
        "CREATE_COMMITMENT": {"beneficiary_npc_id": {"type": "string"}, "commitment_type": {"type": "string"}, "target": {}, "due_world_minute": {"type": "integer", "minimum": 0}},
        "CANCEL_COMMITMENT": {"commitment_id": {"type": "string", "format": "uuid"}}, "PERFORM_COMMITMENT": {"commitment_id": {"type": "string", "format": "uuid"}, "phase": {"type": "string"}},
        "STORY_VOTE": {"meeting_id": {"type": "string", "format": "uuid"}, "plan_code": {"type": "string"}, "expected_story_version": {"type": "integer", "minimum": 0}},
        "STORY_ACT": {"action_code": {"type": "string"}, "expected_story_version": {"type": "integer", "minimum": 0}},
    }
    properties = fields.get(action, {})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": properties, "additionalProperties": False}


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def npc_available_actions(request, npc_id):
    """Frozen P4 route: GET /api/village/npcs/{npc_id}/available-actions."""
    result = runtime_summary(npc_id)
    status = 200 if result["success"] else 409
    available = set(result.get("available_actions", []))
    actions = [
        {
            "name": action,
            "available": action in available,
            "reason": "AVAILABLE" if action in available else "RUNTIME_NOT_READY",
            "state_version": result.get("state_version"),
            "expires_world_minute": result.get("world_minute", 0) + 30,
            "parameter_schema": _discover_parameter_schema(action),
            "result_codes": ["COMMAND_ACCEPTED", "ACTION_NOT_ALLOWED", "NPC_BUSY"],
        }
        for action in sorted(CommandService.ALLOWED_ACTIONS)
    ]
    return Response({
        "success": result["success"], "resultCode": result["result_code"],
        "data": {"npc_id": result.get("npc_id"), "state_version": result.get("state_version"), "available_actions": result.get("available_actions", []), "action_specs": actions},
    }, status=status)


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def npc_discover(request, npc_id):
    """Complete dynamic Discover projection used by the formal Hub path."""
    return npc_available_actions(request, npc_id)


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def events_list(request):
    """GET /api/village/events?after_id=&limit="""
    after_id = request.query_params.get("after_id")
    limit = request.query_params.get("limit", 50)

    try:
        after_id = int(after_id) if after_id else None
        limit = int(limit)
    except ValueError:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA"},
            status=400,
        )

    events = EventService.get_events(after_id=after_id, limit=limit)
    return Response({"success": True, "data": events})


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def commands_accept(request):
    """POST /api/village/commands/accept"""
    envelope_id = request.data.get("envelope_id")
    sender = request.data.get("sender")
    recipient = request.data.get("recipient")
    content = request.data.get("content")
    timestamp_ms = request.data.get("timestamp")

    if not all([envelope_id, sender, recipient, content]) or timestamp_ms is None:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "缺少必要字段"},
            status=400,
        )

    ok, result_code, command = CommandService.accept_command(
        envelope_id=envelope_id,
        sender=sender,
        recipient=recipient,
        content=content,
        timestamp_ms=timestamp_ms,
    )

    if not ok:
        return Response({"success": False, "resultCode": result_code}, status=409)

    return Response({
        "success": True,
        "resultCode": result_code,
        "data": {
            "command_id": str(command.command_id) if command else None,
            "status": command.status if command else None,
        },
    })


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def commands_start(request, command_id):
    """POST /api/village/commands/{command_id}/start"""
    try:
        command_id_uuid = uuid.UUID(command_id)
    except ValueError:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "无效的 command_id"},
            status=400,
        )

    # Unity 是时间权威：start_world_minute 是 Unity 本地实际开始分钟，
    # 用于武装命令区间（与结算区间精确一致），缺失时回退后端本地钟。
    start_world_minute = request.data.get("start_world_minute") if isinstance(request.data, dict) else None
    if start_world_minute is not None:
        try:
            start_world_minute = int(start_world_minute)
        except (TypeError, ValueError):
            return Response(
                {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "无效的 start_world_minute"},
                status=400,
            )

    ok, result_code, command = CommandService.start_command(command_id_uuid, start_world_minute)

    if not ok:
        return Response({"success": False, "resultCode": result_code}, status=409)

    return Response({
        "success": True,
        "resultCode": result_code,
        "data": {
            "command_id": str(command.command_id) if command else None,
            "status": command.status if command else None,
        },
    })


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def commands_status(request, command_id):
    try:
        command_uuid = uuid.UUID(command_id)
    except (TypeError, ValueError):
        return Response({"success": False, "resultCode": ResultCode.INVALID_COMMAND_SCHEMA}, status=400)
    ok, result_code, data = CommandService.get_status(command_uuid)
    return Response({"success": ok, "resultCode": result_code, "data": data}, status=200 if ok else 404)


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def commands_cancel(request, command_id):
    try:
        command_uuid = uuid.UUID(command_id)
    except (TypeError, ValueError):
        return Response({"success": False, "resultCode": ResultCode.INVALID_COMMAND_SCHEMA}, status=400)
    ok, result_code, command = CommandService.cancel_command(command_uuid)
    return Response({
        "success": ok,
        "resultCode": result_code,
        "data": CommandService.serialize_status(command) if command else None,
    }, status=200 if ok else 409)


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def deliveries_pending(request):
    """GET /api/village/deliveries/pending"""
    recipient = request.query_params.get("recipient")
    limit = request.query_params.get("limit", 10)
    after_sequence = request.query_params.get("after_sequence")

    if not recipient:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "缺少 recipient"},
            status=400,
        )

    try:
        limit = int(limit)
        after_sequence = int(after_sequence) if after_sequence is not None else OutboxService.get_cursor(recipient)
    except ValueError:
        limit = 10
        after_sequence = OutboxService.get_cursor(recipient)

    entries = OutboxService.claim_pending(recipient=recipient, limit=limit, after_sequence=after_sequence)
    return Response({"success": True, "data": entries})


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def deliveries_ack(request, delivery_id):
    """POST /api/village/deliveries/{delivery_id}/ack"""
    try:
        delivery_id_uuid = uuid.UUID(delivery_id)
    except ValueError:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "无效的 delivery_id"},
            status=400,
        )

    event_sequence = request.data.get("event_sequence")
    try:
        event_sequence = int(event_sequence) if event_sequence is not None else None
    except (TypeError, ValueError):
        return Response({"success": False, "resultCode": ResultCode.INVALID_COMMAND_SCHEMA}, status=400)
    ok, result_code = OutboxService.ack_delivery(
        delivery_id_uuid,
        event_sequence=event_sequence,
        recipient=request.data.get("recipient"),
    )

    if not ok:
        return Response({"success": False, "resultCode": result_code}, status=409)

    return Response({
        "success": True,
        "resultCode": result_code,
    })


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def deliveries_cursor(request):
    recipient = request.query_params.get("recipient")
    if not recipient:
        return Response({"success": False, "resultCode": ResultCode.INVALID_COMMAND_SCHEMA}, status=400)
    return Response({"success": True, "data": {"recipient": recipient, "last_event_sequence": OutboxService.get_cursor(recipient)}})
