import json
import time
from django.shortcuts import render
from django.db.models import Min
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from config.auth import get_player_id
from .models import PendingReward, ReportEvent, LeaderboardEntry, BoardType
from .serializers import PendingRewardSerializer
from .services import claim_reward, calculate_rankings_realtime
from player.models import PlayerState


def get_current_season_id():
    """Generate a season_id based on current week."""
    from datetime import datetime
    now = datetime.now()
    week = now.isocalendar()[1]
    year = now.year
    return f"week_{year}_{week}"


def get_current_game_day(player_id):
    try:
        player = PlayerState.objects.get(player_id=player_id)
        return player.total_game_days
    except PlayerState.DoesNotExist:
        return 1


def _get_player_game_day(player_id):
    """Return the player's current total_game_days, or None if not found."""
    try:
        state = PlayerState.objects.get(player_id=player_id)
        return state.total_game_days
    except PlayerState.DoesNotExist:
        return None


@api_view(["GET"])
def leaderboard_detail(request, board_type):
    if board_type not in [BoardType.WEALTH, BoardType.OUTPUT, BoardType.FRESHNESS]:
        return Response({"success": False, "message": "invalid board type"}, status=400)

    player_id = get_player_id(request)
    season_id = get_current_season_id()
    current_game_day = get_current_game_day(player_id)

    day_param = request.query_params.get("day")
    if day_param is not None:
        try:
            game_day = int(day_param)
        except (ValueError, TypeError):
            game_day = current_game_day
    else:
        game_day = current_game_day

    entries, my_rank = calculate_rankings_realtime(board_type, player_id, game_day=game_day)
    entries = entries[:50]

    return Response({
        "success": True,
        "message": "OK",
        "data": {
            "boardType": board_type,
            "seasonId": season_id,
            "gameDay": game_day,
            "entries": entries,
            "myRank": my_rank,
        },
    })


@api_view(["GET"])
def leaderboard_my_rank(request, board_type):
    if board_type not in [BoardType.WEALTH, BoardType.OUTPUT, BoardType.FRESHNESS]:
        return Response({"success": False, "message": "invalid board type"}, status=400)

    player_id = get_player_id(request)
    current_game_day = get_current_game_day(player_id)

    day_param = request.query_params.get("day")
    if day_param is not None:
        try:
            game_day = int(day_param)
        except (ValueError, TypeError):
            game_day = current_game_day
    else:
        game_day = current_game_day

    _, my_rank = calculate_rankings_realtime(board_type, player_id, game_day=game_day)

    return Response({
        "success": True,
        "data": my_rank,
    })


@api_view(["GET"])
def leaderboard_summary(request):
    player_id = get_player_id(request)
    season_id = get_current_season_id()
    current_game_day = get_current_game_day(player_id)

    day_param = request.query_params.get("day")
    if day_param is not None:
        try:
            game_day = int(day_param)
        except (ValueError, TypeError):
            game_day = current_game_day
    else:
        game_day = current_game_day

    summaries = []
    for board_type in [BoardType.WEALTH, BoardType.OUTPUT, BoardType.FRESHNESS]:
        entries, my_rank = calculate_rankings_realtime(board_type, player_id, game_day=game_day)
        top_entry = entries[0] if entries else None

        summaries.append({
            "boardType": board_type,
            "topEntry": top_entry,
            "myRank": my_rank,
        })

    return Response({
        "success": True,
        "data": {
            "seasonId": season_id,
            "gameDay": game_day,
            "boards": summaries,
        },
    })


@api_view(["GET"])
def reward_pending(request):
    player_id = get_player_id(request)
    now_ts = int(time.time())

    pending = PendingReward.objects.filter(
        player_id=player_id,
        claimed=False,
        expire_time__gt=now_ts,
    )

    serialized = PendingRewardSerializer(pending, many=True).data

    return Response({
        "success": True,
        "data": {
            "pendingRewards": serialized,
        },
    })


@api_view(["POST"])
def reward_claim(request):
    player_id = get_player_id(request)
    reward_id = request.data.get("rewardId")

    if not reward_id:
        return Response({"success": False, "message": "rewardId required"}, status=400)

    success, message, effects = claim_reward(reward_id, player_id)

    if not success:
        return Response({"success": False, "message": message}, status=400)

    # Return updated AP if applicable
    try:
        player = PlayerState.objects.get(player_id=player_id)
        effects["action_points"] = player.action_points
        effects["action_points_max"] = player.action_points_max
    except PlayerState.DoesNotExist:
        pass

    return Response({
        "success": True,
        "message": "OK",
        "data": effects,
    })


@api_view(["POST"])
def report_heartbeat(request):
    player_id = get_player_id(request)
    timestamp = request.data.get("timestamp", int(time.time()))
    game_day = _get_player_game_day(player_id)

    ReportEvent.objects.create(
        player_id=player_id,
        event_type="heartbeat",
        payload={"timestamp": timestamp},
        timestamp=timestamp,
        game_day=game_day,
    )

    return Response({"success": True, "message": "ok"})


@api_view(["POST"])
def report_event(request):
    player_id = get_player_id(request)
    event_type = request.data.get("eventType", "")
    payload = request.data.get("payload", {})
    timestamp = request.data.get("timestamp", int(time.time()))
    game_day = _get_player_game_day(player_id)

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"raw": payload}

    ReportEvent.objects.create(
        player_id=player_id,
        event_type=event_type,
        payload=payload,
        timestamp=timestamp,
        game_day=game_day,
    )

    return Response({"success": True, "message": "event recorded"})


@api_view(["POST"])
def report_batch(request):
    player_id = get_player_id(request)
    events = request.data.get("events", [])
    game_day = _get_player_game_day(player_id)

    failed_indices = []
    for i, ev in enumerate(events):
        try:
            event_type = ev.get("eventType", "")
            payload = ev.get("payload", {})
            timestamp = ev.get("timestamp", int(time.time()))

            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {"raw": payload}

            ReportEvent.objects.create(
                player_id=player_id,
                event_type=event_type,
                payload=payload,
                timestamp=timestamp,
                game_day=game_day,
            )
        except Exception:
            failed_indices.append(i)

    if failed_indices:
        return Response({
            "success": False,
            "message": f"partial failure: {len(failed_indices)}/{len(events)} events failed",
            "failedIndices": failed_indices,
        })

    return Response({"success": True, "message": f"processed {len(events)} events"})


# ───── 排行榜历史浏览 ─────

def leaderboard_history_page(request):
    """Render the leaderboard history browsing page."""
    return render(request, "leaderboard_history.html")


@api_view(["GET"])
@permission_classes([AllowAny])
def history_days(request):
    """Return a list of game days that have leaderboard data (snapshots + current day)."""
    days = set(
        d for d in LeaderboardEntry.objects.values_list("game_day", flat=True).distinct()
        if d is not None
    )
    # Include the current game day even if no snapshot exists yet
    current = PlayerState.objects.order_by("-total_game_days").values_list("total_game_days", flat=True).first()
    if current is not None:
        days.add(current)
    return Response({
        "success": True,
        "data": {"days": sorted(days, reverse=True)},
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def history_detail(request, board_type):
    """Return leaderboard entries for a given board type and game day.

    Uses real-time calculation from ReportEvent for all days to ensure up-to-date data.
    """
    if board_type not in [BoardType.WEALTH, BoardType.OUTPUT, BoardType.FRESHNESS]:
        return Response({"success": False, "message": "invalid board type"}, status=400)

    day_param = request.query_params.get("day")
    if day_param is None:
        return Response({"success": False, "message": "day parameter required"}, status=400)

    try:
        game_day = int(day_param)
    except (ValueError, TypeError):
        return Response({"success": False, "message": "invalid day"}, status=400)

    entries, _ = calculate_rankings_realtime(board_type, game_day=game_day)

    data = [
        {
            "rank": e["rank"],
            "characterId": e["characterId"],
            "displayName": e["displayName"],
            "isAI": e["isAI"],
            "score": e["score"],
            "title": e.get("title", ""),
            "dishName": e.get("dishName", ""),
        }
        for e in entries[:50]
    ]

    return Response({
        "success": True,
        "data": {
            "boardType": board_type,
            "gameDay": game_day,
            "entries": data,
        },
    })
