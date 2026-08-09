"""服务端唯一权威的全局游戏时间接口。"""
import datetime
import logging

from django.db import transaction
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication
from config.auth import get_player_id
from player.models import PlayerState
from season.season_service import check_and_rotate_season, get_active_season, get_season_notice, transition_player_to_season
from .models import GlobalGameTimeState
from .services import TimeAuthorityService

logger = logging.getLogger(__name__)


def _safe_date(year, month, day):
    year, month = max(1, min(9999, int(year))), max(1, min(12, int(month)))
    following = datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)
    return datetime.date(year, month, max(1, min((following - datetime.timedelta(days=1)).day, int(day))))


def _payload(state, season=None, day_delta=0):
    state = TimeAuthorityService.snapshot(state)
    season = season or get_active_season()
    return {
        "game_year": state.game_year, "game_month": state.game_month, "game_day": state.game_day,
        "game_hour": state.game_hour, "game_minute": state.game_minute, "season": state.season,
        "current_season_id": state.current_season_id, "seasonId": season.season_id,
        "seasonSequence": season.sequence, "seasonEndTime": season.end_time, "day_delta": day_delta,
        "world_minute": state.world_minute, "time_revision": state.time_revision,
    }


@api_view(["POST"])
def time_sync(request):
    """兼容旧客户端：不再接受客户端上报时间，只返回全局快照。"""
    return Response({"success": True, "data": _payload(GlobalGameTimeState.get_instance())})


@api_view(["POST"])
def check_day_advance(request):
    return Response({"success": False, "message": "游戏时间由服务端推进", "data": _payload(GlobalGameTimeState.get_instance())})


@api_view(["POST"])
def day_advanced(request):
    """旧客户端调用不再具备推进权限。"""
    return Response({"success": False, "message": "游戏时间由服务端推进", "data": _payload(GlobalGameTimeState.get_instance())})


@api_view(["GET"])
def game_init(request):
    """登录初始化：返回当前全局时间与本人的行动力、赛季状态。"""
    player, _ = PlayerState.objects.get_or_create(player_id=get_player_id(request))
    active = check_and_rotate_season()
    if not player.current_season_id:
        player.current_season_id = active.season_id
        player.save(update_fields=["current_season_id"])
    from player.services import recover_action_points, NATURAL_ACTION_POINT_CAP
    recover_action_points(player)
    data = _payload(GlobalGameTimeState.get_instance(), active)
    data.update({"action_points": player.action_points, "action_points_max": player.action_points_max,
                 "action_points_natural_max": NATURAL_ACTION_POINT_CAP, "action_points_recovery_seconds": 180})
    notice = get_season_notice(player)
    if notice:
        data["seasonNotice"] = notice
    return Response({"code": 0, "data": data})


@api_view(["POST"])
def season_refresh(request):
    player, _ = PlayerState.objects.get_or_create(player_id=get_player_id(request))
    season = transition_player_to_season(player)
    return Response({"success": True, "data": _payload(GlobalGameTimeState.get_instance(), season)})


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def global_time_get(request):
    return Response({"success": True, "data": _payload(GlobalGameTimeState.get_instance())})


def _apply_time_update(state, data):
    date = _safe_date(data.get("game_year", state.game_year), data.get("game_month", state.game_month), data.get("game_day", state.game_day))
    state.game_year, state.game_month, state.game_day = date.year, date.month, date.day
    state.game_hour = max(0, min(23, int(data.get("game_hour", state.game_hour))))
    state.game_minute = max(0, min(59, int(data.get("game_minute", state.game_minute))))
    state.season = int(data.get("season", state.season))
    active = check_and_rotate_season()
    state.current_season_id = active.season_id
    state.save()
    return active


def _run_time_hooks(state):
    restaurant_updates = []
    # 结算依赖真实世界时间，餐厅依赖全局游戏时间；失败不影响时间权威性。
    try:
        from leaderboard.services import settle_due_periods
        settle_due_periods()
    except Exception:
        logger.exception("periodic leaderboard settlement failed")
    try:
        from restaurant.services import RestaurantService
        restaurant_updates = RestaurantService.process_global_time(state) or []
    except Exception:
        logger.exception("restaurant hourly sales failed")
    return restaurant_updates


@api_view(["PUT"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def global_time_update(request):
    state = GlobalGameTimeState.get_instance()
    # Env identity alone is not a time capability. Calendar writes must carry
    # world_minute and expected_time_revision through Village TimeService CAS.
    return Response({
        "success": False,
        "message": TimeAuthorityService.legacy_mutation_rejected(),
        "data": _payload(state),
    }, status=409)


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
@transaction.atomic
def global_time_day_advanced(request):
    """Env Server 的跨日提交：无最大天数、无累计天数限制。"""
    state = GlobalGameTimeState.get_instance()
    return Response({
        "success": False,
        "message": TimeAuthorityService.legacy_mutation_rejected(),
        "data": _payload(state),
    }, status=409)
