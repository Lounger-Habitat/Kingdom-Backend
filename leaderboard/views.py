import json
import time

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from config.auth import get_player_id
from inventory.models import InventoryBag
from inventory.serializers import serialize_bag
from player.models import PlayerState
from .models import BoardType, LeaderboardEntry, PendingReward, ReportEvent, RewardConfig, LeaderboardPeriodConfig
from .serializers import PendingRewardSerializer
from .services import claim_reward, get_period_bounds, calculate_period_rankings, calculate_rankings_realtime


def _season_id(player_id):
    from season.season_service import get_active_season
    state, _ = PlayerState.objects.get_or_create(player_id=player_id)
    if not state.current_season_id:
        state.current_season_id = get_active_season().season_id
        state.save(update_fields=["current_season_id"])
    return state.current_season_id


def _entries(board_type, season_id, period_id=None):
    qs = LeaderboardEntry.objects.filter(board_type=board_type, season_id=season_id)
    if period_id:
        qs = qs.filter(period_key=period_id)
    else:
        config = LeaderboardPeriodConfig.objects.filter(enabled=True).order_by("interval_hours").first()
        if config:
            start, end = get_period_bounds(config)
            key = f"{config.pk}:{start.strftime('%Y%m%d%H')}"
            calculate_period_rankings(config, season_id, start, end)
            qs = qs.filter(period_key=key)
    return qs.order_by("rank")


def _leaderboard_detail_data(request, board_type):
    if board_type not in BoardType.values:
        return None, Response({"success": False, "message": "invalid board type"}, status=400)
    player_id, season_id = get_player_id(request), _season_id(get_player_id(request))
    data, mine = calculate_rankings_realtime(board_type, player_id=player_id, season_id=season_id)
    data = data[:50]
    return {"boardType": board_type, "seasonId": season_id, "entries": data, "myRank": mine}, None


@api_view(["GET"])
def leaderboard_detail(request, board_type):
    result, err = _leaderboard_detail_data(request, board_type)
    if err:
        return err
    return Response({"success": True, "data": result})


@api_view(["GET"])
def leaderboard_my_rank(request, board_type):
    result, err = _leaderboard_detail_data(request, board_type)
    if err:
        return err
    return Response({"success": True, "data": result["myRank"]})


@api_view(["GET"])
def leaderboard_summary(request):
    player_id, season_id = get_player_id(request), _season_id(get_player_id(request))
    boards = []
    for board_type in BoardType.values:
        entries, mine = calculate_rankings_realtime(board_type, player_id=player_id, season_id=season_id)
        top = entries[0] if entries else None
        boards.append({"boardType": board_type,
            "topEntry": None if not top else {"rank": top["rank"], "characterId": top["characterId"], "displayName": top["displayName"], "score": top["score"]},
            "myRank": mine})
    return Response({"success": True, "data": {"seasonId": season_id, "boards": boards}})


@api_view(["GET"])
def reward_pending(request):
    player_id = get_player_id(request)
    rows = PendingReward.objects.filter(player_id=player_id, claimed=False, expire_time__gt=int(time.time()))
    return Response({"success": True, "data": {"pendingRewards": PendingRewardSerializer(rows, many=True).data}})


@api_view(["POST"])
def reward_claim(request):
    player_id = get_player_id(request)
    ok, message, effects = claim_reward(request.data.get("rewardId", ""), player_id)
    if not ok:
        return Response({"success": False, "message": message, "data": effects}, status=400)

    player = PlayerState.objects.get(player_id=player_id)
    bag = InventoryBag.objects.filter(
        player=request.user,
        character_id=getattr(request.user, "username", ""),
    ).first()
    data = dict(effects or {})
    data.update({
        "actionPoints": player.action_points,
        "actionPointsMax": player.action_points_max,
        "bagData": serialize_bag(bag) if bag is not None else None,
    })
    return Response({"success": True, "message": message, "data": data})


def _save_event(player_id, event):
    payload = event.get("payload", {})
    if isinstance(payload, str):
        try: payload = json.loads(payload)
        except json.JSONDecodeError: payload = {"raw": payload}
    ReportEvent.objects.create(player_id=player_id, event_type=event.get("eventType", ""), payload=payload,
        timestamp=int(event.get("timestamp", time.time())), season_id=_season_id(player_id))


@api_view(["POST"])
def report_event(request):
    _save_event(get_player_id(request), request.data)
    return Response({"success": True, "message": "event recorded"})


@api_view(["POST"])
def report_batch(request):
    for event in request.data.get("events", []):
        _save_event(get_player_id(request), event)
    return Response({"success": True, "message": "events recorded"})


@api_view(["POST"])
def report_heartbeat(request):
    _save_event(get_player_id(request), {"eventType": "heartbeat", "timestamp": request.data.get("timestamp", time.time())})
    return Response({"success": True})


@api_view(["GET"])
def season_info(request):
    from season.season_service import get_active_season
    season = get_active_season()
    return Response({"success": True, "data": {"seasonId": season.season_id, "sequence": season.sequence, "startTime": season.start_time, "endTime": season.end_time}})


@api_view(["GET"])
def reward_tips(request):
    configs = RewardConfig.objects.filter(period_config_id=request.query_params.get("period_id") or None).order_by("rank_min")
    return Response({"success": True, "data": [{"rank_min": c.rank_min, "rank_max": c.rank_max, "reward_type": c.reward_type, "reward_data": c.reward_data} for c in configs]})


@api_view(["GET"])
@permission_classes([AllowAny])
def history_days(request):
    return Response({"success": True, "data": {"periods": list(LeaderboardEntry.objects.exclude(period_key="").values_list("period_key", flat=True).distinct())}})


@api_view(["GET"])
@permission_classes([AllowAny])
def history_detail(request, board_type):
    return leaderboard_detail(request, board_type)


def leaderboard_history_page(request):
    from django.shortcuts import render
    return render(request, "leaderboard_history.html")
