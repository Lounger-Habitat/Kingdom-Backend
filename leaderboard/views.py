import json
import time
from django.shortcuts import render
from django.db.models import Min
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from config.auth import get_player_id
from .models import PendingReward, ReportEvent, LeaderboardEntry, BoardType, RewardConfig
from .serializers import PendingRewardSerializer
from .services import claim_reward, refresh_leaderboard
from player.models import PlayerState
from account.models import GameAccount


def get_current_season_id():
    """Return the current active season_id from the Season table."""
    from .season_service import get_active_season
    return get_active_season().season_id


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


def _get_player_season_id(player_id):
    """Return the player's current_season_id, falling back to active season."""
    try:
        state = PlayerState.objects.get(player_id=player_id)
        if state.current_season_id:
            return state.current_season_id
    except PlayerState.DoesNotExist:
        pass
    return get_current_season_id()


@api_view(["GET"])
def leaderboard_detail(request, board_type):
    if board_type not in BoardType.values:
        return Response({"success": False, "message": "invalid board type"}, status=400)

    player_id = get_player_id(request)
    season_id = _get_player_season_id(player_id)
    current_game_day = get_current_game_day(player_id)

    is_total = board_type.startswith("total_")
    if is_total:
        game_day = None
    else:
        day_param = request.query_params.get("day")
        if day_param is not None:
            try:
                game_day = int(day_param)
            except (ValueError, TypeError):
                game_day = current_game_day
        else:
            game_day = current_game_day

    # 从 LeaderboardEntry 读取
    qs = LeaderboardEntry.objects.filter(board_type=board_type, season_id=season_id)
    if game_day is not None:
        qs = qs.filter(game_day=game_day)
    else:
        qs = qs.filter(game_day__isnull=True)

    raw_entries = list(qs.order_by("rank")[:50])
    ai_flags = {
        str(acc.id): acc.is_ai
        for acc in GameAccount.objects.filter(id__in=[e.character_id for e in raw_entries])
    }

    entries = []
    my_rank = None
    for e in raw_entries:
        entry = {
            "rank": e.rank,
            "characterId": e.character_id,
            "displayName": e.display_name,
            "isAI": ai_flags.get(e.character_id, False),
            "score": e.score,
            "title": e.title or "",
            "avatarId": e.avatar_id or "",
            "dishName": e.dish_name or "",
        }
        entries.append(entry)
        if e.character_id == player_id and my_rank is None:
            my_rank = {"rank": e.rank, "score": e.score}

    return Response({
        "success": True,
        "message": "OK",
        "data": {
            "boardType": board_type,
            "seasonId": season_id,
            "gameDay": game_day if game_day is not None else current_game_day,
            "entries": entries,
            "myRank": my_rank,
        },
    })


@api_view(["GET"])
def leaderboard_my_rank(request, board_type):
    if board_type not in BoardType.values:
        return Response({"success": False, "message": "invalid board type"}, status=400)

    player_id = get_player_id(request)
    season_id = _get_player_season_id(player_id)
    current_game_day = get_current_game_day(player_id)

    is_total = board_type.startswith("total_")
    if is_total:
        game_day = None
    else:
        day_param = request.query_params.get("day")
        if day_param is not None:
            try:
                game_day = int(day_param)
            except (ValueError, TypeError):
                game_day = current_game_day
        else:
            game_day = current_game_day

    qs = LeaderboardEntry.objects.filter(board_type=board_type, season_id=season_id, character_id=player_id)
    if game_day is not None:
        qs = qs.filter(game_day=game_day)
    else:
        qs = qs.filter(game_day__isnull=True)

    entry = qs.first()
    my_rank = {"rank": entry.rank, "score": entry.score} if entry else None

    return Response({
        "success": True,
        "data": my_rank,
    })


@api_view(["GET"])
def leaderboard_summary(request):
    player_id = get_player_id(request)
    season_id = _get_player_season_id(player_id)
    current_game_day = get_current_game_day(player_id)

    day_param = request.query_params.get("day")
    if day_param is not None:
        try:
            game_day = int(day_param)
        except (ValueError, TypeError):
            game_day = current_game_day
    else:
        game_day = current_game_day

    # Pre-fetch top entries for all board types
    top_entries_by_board = {}
    for board_type in BoardType.values:
        is_total = board_type.startswith("total_")
        qs = LeaderboardEntry.objects.filter(board_type=board_type, season_id=season_id)
        if is_total:
            qs = qs.filter(game_day__isnull=True)
        else:
            qs = qs.filter(game_day=game_day)
        top_entries_by_board[board_type] = qs.order_by("rank").first()

    # Batch fetch AI flags from GameAccount
    top_ids = [e.character_id for e in top_entries_by_board.values() if e]
    ai_flags = {
        str(acc.id): acc.is_ai
        for acc in GameAccount.objects.filter(id__in=top_ids)
    }

    summaries = []
    for board_type in BoardType.values:
        is_total = board_type.startswith("total_")
        qs = LeaderboardEntry.objects.filter(board_type=board_type, season_id=season_id)
        if is_total:
            qs = qs.filter(game_day__isnull=True)
        else:
            qs = qs.filter(game_day=game_day)

        top_entry_obj = top_entries_by_board[board_type]
        top_entry = None
        if top_entry_obj:
            top_entry = {
                "rank": top_entry_obj.rank,
                "characterId": top_entry_obj.character_id,
                "displayName": top_entry_obj.display_name,
                "isAI": ai_flags.get(top_entry_obj.character_id, False),
                "score": top_entry_obj.score,
                "title": top_entry_obj.title or "",
                "dishName": top_entry_obj.dish_name or "",
            }

        my_entry = qs.filter(character_id=player_id).first()
        my_rank = {"rank": my_entry.rank, "score": my_entry.score} if my_entry else None

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
    season_id = _get_player_season_id(player_id)

    ReportEvent.objects.create(
        player_id=player_id,
        event_type="heartbeat",
        payload={"timestamp": timestamp},
        timestamp=timestamp,
        game_day=game_day,
        season_id=season_id,
    )

    return Response({"success": True, "message": "ok"})


@api_view(["POST"])
def report_event(request):
    player_id = get_player_id(request)
    event_type = request.data.get("eventType", "")
    payload = request.data.get("payload", {})
    timestamp = request.data.get("timestamp", int(time.time()))
    game_day = _get_player_game_day(player_id)
    season_id = _get_player_season_id(player_id)

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
        season_id=season_id,
    )

    # 实时刷新对应的 LeaderboardEntry
    if event_type == "cook_completed":
        refresh_leaderboard(BoardType.TOTAL_OUTPUT, season_id)
    elif event_type == "judge_confirmed":
        refresh_leaderboard(BoardType.TOTAL_FRESHNESS, season_id)

    return Response({"success": True, "message": "event recorded"})


@api_view(["POST"])
def report_batch(request):
    player_id = get_player_id(request)
    events = request.data.get("events", [])
    game_day = _get_player_game_day(player_id)
    season_id = _get_player_season_id(player_id)

    failed_indices = []
    event_types_seen = set()
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
                season_id=season_id,
            )
            event_types_seen.add(event_type)
        except Exception:
            failed_indices.append(i)

    # 批量上报完成后，统一刷新一次对应的 LeaderboardEntry
    if "cook_completed" in event_types_seen:
        refresh_leaderboard(BoardType.TOTAL_OUTPUT, season_id)
    if "judge_confirmed" in event_types_seen:
        refresh_leaderboard(BoardType.TOTAL_FRESHNESS, season_id)

    if failed_indices:
        return Response({
            "success": False,
            "message": f"partial failure: {len(failed_indices)}/{len(events)} events failed",
            "failedIndices": failed_indices,
        })

    return Response({"success": True, "message": f"processed {len(events)} events"})


@api_view(["GET"])
def season_info(request):
    """Return current season details for the requesting player."""
    from .season_service import get_active_season

    player_id = get_player_id(request)
    season = get_active_season()
    now = int(time.time())

    player_game_day = 1
    player_season_id = season.season_id
    try:
        player = PlayerState.objects.get(player_id=player_id)
        player_game_day = player.total_game_days
        if player.current_season_id:
            player_season_id = player.current_season_id
    except PlayerState.DoesNotExist:
        pass

    return Response({
        "success": True,
        "data": {
            "seasonId": season.season_id,
            "playerSeasonId": player_season_id,
            "sequence": season.sequence,
            "startTime": season.start_time,
            "endTime": season.end_time,
            "durationDays": season.duration_days,
            "remainingSeconds": max(0, season.end_time - now),
            "gameDay": player_game_day,
        },
    })


@api_view(["GET"])
def reward_tips(request):
    """Return reward config entries for a given board_type + settlement_type."""
    board_type = request.query_params.get("board_type", "")
    settlement_type = request.query_params.get("settlement_type", "")

    if board_type not in BoardType.values:
        return Response({"success": False, "message": "invalid board_type"}, status=400)

    configs = RewardConfig.objects.filter(
        board_type=board_type,
        settlement_type=settlement_type,
    ).order_by("rank_min")

    # Collect item IDs to batch lookup names
    item_ids = set()
    for cfg in configs:
        rd = cfg.reward_data or {}
        if cfg.reward_type == "item" and rd.get("itemId"):
            item_ids.add(rd["itemId"])

    item_names = {}
    if item_ids:
        from items.models import Item
        for item in Item.objects.filter(item_id__in=item_ids):
            item_names[item.item_id] = item.item_name

    data = []
    for cfg in configs:
        rd = cfg.reward_data or {}
        entry = {
            "rank_min": cfg.rank_min,
            "rank_max": cfg.rank_max,
            "reward_type": cfg.reward_type,
            "reward_data": rd,
        }
        if cfg.reward_type == "item" and rd.get("itemId"):
            entry["itemName"] = item_names.get(rd["itemId"], "")
        data.append(entry)

    # 结算周期描述
    if settlement_type == "daily":
        settlement_info = "每日结算"
    elif settlement_type == "milestone":
        from season.models import SeasonConfig
        n = SeasonConfig.get_instance().milestone_days
        settlement_info = f"每{n}游戏日结算"
    elif settlement_type == "season":
        settlement_info = "赛季结算"
    else:
        settlement_info = ""

    return Response({"success": True, "data": data, "settlementInfo": settlement_info})


# ───── 排行榜历史浏览 ─────

def leaderboard_history_page(request):
    """Render the leaderboard history browsing page."""
    return render(request, "leaderboard_history.html")


@api_view(["GET"])
@permission_classes([AllowAny])
def history_days(request):
    """Return a list of game days that have leaderboard data (snapshots + current day)."""
    qs = LeaderboardEntry.objects.all()
    season_param = request.query_params.get("season")
    if season_param:
        qs = qs.filter(season_id=season_param)

    days = set(
        d for d in qs.values_list("game_day", flat=True).distinct()
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
    """Return leaderboard entries for a given board type and game day."""
    if board_type not in BoardType.values:
        return Response({"success": False, "message": "invalid board type"}, status=400)

    season_param = request.query_params.get("season")
    day_param = request.query_params.get("day")
    is_total = board_type.startswith("total_")

    qs = LeaderboardEntry.objects.filter(board_type=board_type)
    if season_param:
        qs = qs.filter(season_id=season_param)

    if is_total:
        game_day = None
        qs = qs.filter(game_day__isnull=True)
    elif day_param is not None:
        try:
            game_day = int(day_param)
        except (ValueError, TypeError):
            return Response({"success": False, "message": "invalid day"}, status=400)
        qs = qs.filter(game_day=game_day)
    else:
        return Response({"success": False, "message": "day parameter required"}, status=400)

    raw_entries = list(qs.order_by("rank")[:50])
    ai_flags = {
        str(acc.id): acc.is_ai
        for acc in GameAccount.objects.filter(id__in=[e.character_id for e in raw_entries])
    }

    data = [
        {
            "rank": e.rank,
            "characterId": e.character_id,
            "displayName": e.display_name,
            "isAI": ai_flags.get(e.character_id, False),
            "score": e.score,
            "title": e.title or "",
            "dishName": e.dish_name or "",
        }
        for e in raw_entries
    ]

    return Response({
        "success": True,
        "data": {
            "boardType": board_type,
            "gameDay": game_day if game_day is not None else 0,
            "entries": data,
        },
    })
