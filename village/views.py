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
    TimeService,
)
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
    """PUT /api/village/runtime/checkpoint"""
    checkpoints = request.data.get("checkpoints", [])
    if not checkpoints:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "checkpoints 不能为空"},
            status=400,
        )

    results = CheckpointService.submit_checkpoint(checkpoints)
    all_success = all(r.get("success") for r in results)

    return Response({
        "success": all_success,
        "results": results,
    })


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
    success = request.data.get("success", True)
    result_code = request.data.get("result_code", "")
    message = request.data.get("message", "")
    data = request.data.get("data", {})

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
            "state_version": receipt.state_version if receipt else None,
        },
    })


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

    if not all([envelope_id, sender, recipient, content]):
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "缺少必要字段"},
            status=400,
        )

    ok, result_code, command = CommandService.accept_command(
        envelope_id=envelope_id,
        sender=sender,
        recipient=recipient,
        content=content,
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

    ok, result_code, command = CommandService.start_command(command_id_uuid)

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
def deliveries_pending(request):
    """GET /api/village/deliveries/pending"""
    recipient = request.query_params.get("recipient")
    limit = request.query_params.get("limit", 10)

    if not recipient:
        return Response(
            {"success": False, "resultCode": "INVALID_COMMAND_SCHEMA", "message": "缺少 recipient"},
            status=400,
        )

    try:
        limit = int(limit)
    except ValueError:
        limit = 10

    entries = OutboxService.claim_pending(recipient=recipient, limit=limit)
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

    ok, result_code = OutboxService.ack_delivery(delivery_id_uuid)

    if not ok:
        return Response({"success": False, "resultCode": result_code}, status=409)

    return Response({
        "success": True,
        "resultCode": result_code,
    })
