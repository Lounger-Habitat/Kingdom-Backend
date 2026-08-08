from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication

from .models import NpcProfile, VillageLocationDefinition, VillageSimulationConfig
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
