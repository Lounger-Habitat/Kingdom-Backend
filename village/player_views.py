from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication
from .player_actions import execute_player_action
from .player_affair_labor import tick_player_labor


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def player_action(request):
    result = execute_player_action(request.data)
    return Response(result, status=409 if result["result_code"] == "REQUEST_CONFLICT" else 200)


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def player_labor_tick(request):
    return Response(tick_player_labor(request.data))
