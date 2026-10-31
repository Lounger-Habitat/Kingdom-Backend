from functools import wraps
from time import perf_counter

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication
from .player_actions import execute_player_action
from .player_affair_labor import tick_player_labor


def _time_request(view):
    @wraps(view)
    def timed(*args, **kwargs):
        started = perf_counter()
        response = view(*args, **kwargs)
        if "Server-Timing" in response:
            response["Server-Timing"] += f", request;dur={(perf_counter() - started) * 1000:.1f}"
        return response
    return timed


@_time_request
@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def player_action(request):
    timings = {}
    result = execute_player_action(request.data, timings=timings)
    response = Response(result, status=409 if result["result_code"] == "REQUEST_CONFLICT" else 200)
    if timings:
        response["Server-Timing"] = ", ".join(f"{name};dur={duration:.1f}" for name, duration in timings.items())
    return response


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def player_labor_tick(request):
    return Response(tick_player_labor(request.data))
