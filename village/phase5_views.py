from __future__ import annotations

import uuid

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication
from config.auth import get_player_id
from .phase5_story import dispatch_story_command, get_story_snapshot, settle_story_meeting


def _response(result):
    return Response(result, status=409 if result.get("result_code") in {
        "STORY_VERSION_CONFLICT", "STORY_REQUEST_CONFLICT", "STORY_ROUTE_ATTEMPT_CONFLICT",
    } else 200)


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def story_snapshot(request):
    return _response(get_story_snapshot())


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def story_command(request):
    if not isinstance(request.data, dict):
        return Response({"result_code": "STORY_ACTION_INVALID"}, status=400)
    payload = dict(request.data)
    player_id = get_player_id(request) if str(payload.get("actor_type", "")).upper() == "PLAYER" else None
    return _response(dispatch_story_command(payload, player_id=player_id))


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def story_vote(request):
    if not isinstance(request.data, dict):
        return Response({"result_code": "STORY_ACTION_INVALID"}, status=400)
    payload = dict(request.data)
    payload["command"] = "STORY_VOTE"
    player_id = get_player_id(request) if str(payload.get("actor_type", "")).upper() == "PLAYER" else None
    return _response(dispatch_story_command(payload, player_id=player_id))


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def story_action_complete(request):
    if not isinstance(request.data, dict):
        return Response({"result_code": "STORY_ACTION_INVALID"}, status=400)
    payload = dict(request.data)
    payload["command"] = "STORY_ACT"
    player_id = get_player_id(request) if str(payload.get("actor_type", "")).upper() == "PLAYER" else None
    return _response(dispatch_story_command(payload, player_id=player_id))


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def story_meeting_settle(request, meeting_id):
    try:
        result = settle_story_meeting(
            uuid.UUID(str(meeting_id)),
            expected_story_version=request.data.get("expected_story_version"),
            request_id=request.data.get("request_id"),
        )
    except (TypeError, ValueError):
        return Response({"result_code": "STORY_ACTION_INVALID"}, status=400)
    return _response(result)
