import uuid

from django.db.models import Q

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication

from .command_catalog import COMMAND_CATALOG, agent_tool
from .models import (
    NpcProfile,
    VillageLocationDefinition,
    VillageSimulationConfig,
)
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
from .phase2_services import LifeActionService, NeedsService, TickService, needs_rules, runtime_summary
from .goal_services import update_subjective_goal
from .agent_queries import execute_agent_query
from .dialogue_service import DialogueService
from .services import validate_bootstrap


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
            "villagePresentationEnabled": config.village_presentation_enabled,
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
    """Persist only recovery-safe Env fields through the existing CAS service."""
    checkpoints = request.data.get("checkpoints") if isinstance(request.data, dict) else None
    results = CheckpointService.submit_checkpoint(checkpoints)
    success = bool(results) and all(result.get("success") for result in results)
    result_code = (
        ResultCode.COMMAND_ACCEPTED
        if success
        else next(
            (result.get("result_code") for result in results if not result.get("success")),
            ResultCode.INVALID_COMMAND_SCHEMA,
        )
    )
    return Response({
        "success": success,
        "resultCode": result_code,
        "results": results,
    }, status=200 if success else 400)


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def subjective_goal_update(request):
    result = update_subjective_goal(request.data)
    code = result["result_code"]
    if result["success"]:
        http_status = 200
    elif code in {"GOAL_VERSION_CONFLICT", "GOAL_REQUEST_CONFLICT"}:
        http_status = 409
    elif code == "NPC_NOT_FOUND":
        http_status = 404
    else:
        http_status = 400
    return Response({
        "success": result["success"],
        "resultCode": code,
        "data": result["data"],
    }, status=http_status)


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


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def runtime_tick(request):
    """Persist Unity's authoritative time and catch Backend projections up."""
    if not isinstance(request.data, dict):
        return Response({"success": False, "resultCode": ResultCode.INVALID_COMMAND_SCHEMA}, status=400)
    result = TickService.advance(dict(request.data))
    status = 200 if result["success"] else 409
    data = {key: value for key, value in result.items()
            if key not in {"receipt", "success", "result_code"}}
    return Response({"success": result["success"], "resultCode": result["result_code"], "data": data}, status=status)


def _dialogue_response(result):
    code = result["result_code"]
    status = 200 if result["success"] else 404 if code == ResultCode.DIALOGUE_NOT_FOUND else 409
    data = {key: value for key, value in result.items() if key not in {"success", "result_code"}}
    return Response({"success": result["success"], "resultCode": code, "data": data}, status=status)


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def dialogue_start(request):
    if not isinstance(request.data, dict) or not set(request.data).issubset({
        "npc_id", "target_npc_id", "topic"
    }):
        return _dialogue_response({"success": False, "result_code": ResultCode.INVALID_COMMAND_SCHEMA})
    return _dialogue_response(DialogueService.start(
        request.data.get("npc_id"), request.data.get("target_npc_id"), request.data.get("topic", "")
    ))


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def dialogue_speak(request, conversation_id):
    if not isinstance(request.data, dict) or not set(request.data).issubset({
        "npc_id", "text", "end_turn"
    }):
        return _dialogue_response({"success": False, "result_code": ResultCode.INVALID_COMMAND_SCHEMA})
    return _dialogue_response(DialogueService.speak(
        conversation_id, request.data.get("npc_id"), request.data.get("text"),
        request.data.get("end_turn", False),
    ))


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def dialogue_complete(request, conversation_id):
    if not isinstance(request.data, dict) or not set(request.data).issubset({"npc_id", "reason"}):
        return _dialogue_response({"success": False, "result_code": ResultCode.INVALID_COMMAND_SCHEMA})
    return _dialogue_response(DialogueService.complete(
        conversation_id, request.data.get("npc_id"), request.data.get("reason", "COMPLETED")
    ))


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
    return COMMAND_CATALOG.get(action, {"parameters": {}})["parameters"]


def _agent_tool(action):
    return agent_tool(action)


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def npc_available_actions(request, npc_id):
    """Frozen P4 route: GET /api/village/npcs/{npc_id}/available-actions."""
    result = runtime_summary(npc_id)
    status = 200 if result["success"] else 409
    profile = NpcProfile.objects.select_related("npc").filter(
        npc__agent_id=npc_id,
        npc__is_test_only=False,
        is_active=True,
    ).first()
    configured = CommandService.allowed_actions_for(profile.npc) if profile else set()
    configured.discard("SOCIALIZE")
    available = set(result.get("available_actions", []))
    unavailable_reasons = result.get("unavailable_reasons", {})
    sleeping = result.get("action_status") == "SLEEPING" and result.get("active_command_id")
    actions = [
        {
            "name": action,
            "available": action in available,
            "reason": "AVAILABLE" if action in available else unavailable_reasons.get(action, "RUNTIME_NOT_READY"),
            "state_version": result.get("state_version"),
            "expires_world_minute": result.get("world_minute", 0) + 30,
            "parameter_schema": _discover_parameter_schema(action),
            "result_codes": ["COMMAND_ACCEPTED", "ACTION_NOT_ALLOWED", "NPC_BUSY"],
        }
        for action in sorted(configured)
    ]
    tools = [_agent_tool(action) for action in sorted(configured)]
    if "SLEEP" in configured:
        actions.append({
            "name": "WAKE_UP",
            "available": bool(sleeping),
            "reason": "AVAILABLE" if sleeping else ResultCode.NPC_NOT_SLEEPING,
            "state_version": result.get("state_version"),
            "expires_world_minute": result.get("world_minute", 0) + 30,
            "parameter_schema": {
                "type": "object",
                "properties": {
                    "sleep_command_id": {"type": "string", "format": "uuid"},
                },
                "required": ["sleep_command_id"],
            },
            "result_codes": [ResultCode.SLEEP_INTERRUPTED, ResultCode.NPC_NOT_SLEEPING,
                             ResultCode.SLEEP_COMMAND_MISMATCH],
        })
        tools.append({
            "name": "WAKE_UP",
            "description": "立即结束当前睡眠，并只结算实际已睡的世界分钟。",
            "parameters": actions[-1]["parameter_schema"],
        })
    profile_location_codes = set() if profile is None else {
        profile.home_location_code, profile.work_location_code, profile.social_location_code,
    }
    locations = [{
        "location_code": location.location_code,
        "name": location.display_name,
        "location_type": location.location_type,
    } for location in VillageLocationDefinition.objects.filter(is_active=True).filter(
        Q(is_public=True, required_story_states=[]) | Q(location_code__in=profile_location_codes)
    ).order_by("location_code")]
    npc_profile = None if profile is None else {
        "npc_id": str(profile.npc.agent_id),
        "name": profile.display_name,
        "occupation": profile.occupation,
        "home_location": profile.home_location_code,
        "work_location": profile.work_location_code,
        "social_location": profile.social_location_code,
    }
    return Response({
        "success": result["success"], "resultCode": result["result_code"],
        "data": {
            "npc_id": result.get("npc_id"),
            "state_version": result.get("state_version"),
            "world_minute": result.get("world_minute"),
            "action_status": result.get("action_status"),
            "current_action": result.get("current_action"),
            "action_started_world_minute": result.get("action_started_world_minute"),
            "action_ends_world_minute": result.get("action_ends_world_minute"),
            "remaining_minutes": result.get("remaining_minutes"),
            "active_command_id": result.get("active_command_id"),
            "public_needs": result.get("public_needs"),
            "need_bands": result.get("need_bands"),
            "needs_rules": needs_rules(),
            "available_actions": sorted(available | ({"WAKE_UP"} if sleeping else set())),
            "action_specs": actions,
            "npc_profile": npc_profile,
            "locations": locations,
            "tools": tools,
        },
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
def npc_agent_query(request, npc_id, query_name):
    """Execute a capability-gated, read-only Agent query."""
    success, result_code, data = execute_agent_query(npc_id, query_name, request.query_params)
    status = 200 if success else 404 if result_code in {"NPC_NOT_FOUND", "UNKNOWN_QUERY"} else 409
    return Response({"success": success, "resultCode": result_code, "data": data}, status=status)


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


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def interrupt_sleep(request, command_id):
    result = LifeActionService.interrupt_sleep(request.data.get("npc_id", ""), command_id)
    return Response({
        "success": result["success"],
        "resultCode": result["result_code"],
        "data": {key: value for key, value in result.items() if key not in {"success", "result_code", "receipt"}},
    }, status=200 if result["success"] else 409)


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
