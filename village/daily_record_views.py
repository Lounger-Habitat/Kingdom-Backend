from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.authentication import GameJWTAuthentication
from account.env_auth import ENV_SERVER_SCOPE, EnvServerJWTAuthentication
from config.auth import get_player_id
from .daily_records import query_daily_records, write_daily_record
from .models import NpcProfile
from .phase7_services import presentation_enabled


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def write_record(request):
    result = write_daily_record(request.data)
    code = result["result_code"]
    status = 200 if result["success"] else 409 if code.endswith(("CONFLICT", "ALREADY_EXISTS")) else 404 if code.endswith("NOT_FOUND") else 400
    return Response({"success": result["success"], "resultCode": code, "data": result["data"]}, status=status)


@api_view(["GET"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
def public_records(request, npc_id, kind):
    if (request.auth and request.auth.get("scope") == ENV_SERVER_SCOPE) or not get_player_id(request):
        return Response({"success": False, "resultCode": "PLAYER_AUTH_REQUIRED"}, status=403)
    if not presentation_enabled():
        return Response({"success": False, "resultCode": "VILLAGE_PRESENTATION_DISABLED"}, status=404)
    profile = NpcProfile.objects.select_related("npc").filter(
        npc__agent_id=npc_id, npc__is_test_only=False, is_active=True,
    ).first()
    if profile is None:
        return Response({"success": False, "resultCode": "NPC_NOT_FOUND"}, status=404)
    try:
        data = query_daily_records(profile.npc, kind.upper(), request.query_params)
    except ValueError:
        return Response({"success": False, "resultCode": "INVALID_QUERY_PARAMS"}, status=400)
    return Response({"success": True, "data": data})
