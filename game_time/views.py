import datetime
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from config.auth import get_player_id
from player.models import PlayerState
from leaderboard.services import settle_leaderboard_for_day, reset_player_action_points
from leaderboard.views import get_current_season_id
from .models import GameTimeState


logger = logging.getLogger(__name__)


def _safe_date(year, month, day):
    """用真实公历构造 date；越界时夹紧到合法范围，避免 ValueError 抛 500。"""
    year = max(1, min(9999, int(year)))
    month = max(1, min(12, int(month)))
    # 计算该年该月的最大日数
    if month == 12:
        next_month_first = datetime.date(year + 1, 1, 1)
    else:
        next_month_first = datetime.date(year, month + 1, 1)
    last_day = (next_month_first - datetime.timedelta(days=1)).day
    day = max(1, min(last_day, int(day)))
    return datetime.date(year, month, day)


@api_view(["POST"])
def check_day_advance(request):
    """预检查是否允许推进下一天，不修改任何数据。"""
    player_id = get_player_id(request)
    player, _ = PlayerState.objects.get_or_create(player_id=player_id)

    game_time_state = GameTimeState.get_instance()
    max_days = game_time_state.max_game_days

    if max_days > 0 and player.total_game_days >= max_days:
        return Response({
            "success": False,
            "message": f"当前已达到最大天数（第{max_days}天），无法继续推进",
        })

    return Response({"success": True, "message": "ok"})


@api_view(["POST"])
def day_advanced(request):
    """客户端日历日推进时调用。后端按真实公历的日期差累加 total_game_days。

    请求字段（均为日历语义）：
        game_year, game_month, game_day(=day-of-month), season

    响应 data：
        game_year, game_month, game_day, season,
        total_game_days, day_delta, action_points
    """
    player_id = get_player_id(request)

    new_year = int(request.data.get("game_year", 2026))
    new_month = int(request.data.get("game_month", 3))
    new_day = int(request.data.get("game_day", 8))
    new_hour = int(request.data.get("game_hour", 7))
    new_minute = int(request.data.get("game_minute", 0))
    season = int(request.data.get("season", 0))

    new_date = _safe_date(new_year, new_month, new_day)

    player, _ = PlayerState.objects.get_or_create(player_id=player_id)

    # 全服最大天数限制（仅阻止实际天数推进，不影响时间同步）
    game_time_state = GameTimeState.get_instance()
    max_days = game_time_state.max_game_days

    # 模型默认值（2026-3-8, total_game_days=1）即作为新玩家起点：
    # - 客户端首次以 3-8 上报 → delta=0（同日，幂等），仍是第 1 天；
    # - 客户端推进到 3-9 上报 → delta=1，进入第 2 天。
    old_date = _safe_date(player.game_year, player.game_month, player.game_day)
    raw_delta = (new_date - old_date).days
    if raw_delta < 0:
        logger.warning(
            "[day_advanced] player=%s 日历回退 old=%s new=%s，按 0 处理",
            player_id, old_date, new_date,
        )
        day_delta = 0
    else:
        day_delta = raw_delta

    # 全服最大天数限制：有实际天数推进且已达上限时拒绝，返回当前状态供客户端回退
    if day_delta > 0 and max_days > 0 and player.total_game_days >= max_days:
        return Response({
            "success": False,
            "message": f"当前已达到最大天数（第{max_days}天），无法继续推进",
            "data": {
                "game_year": player.game_year,
                "game_month": player.game_month,
                "game_day": player.game_day,
                "game_hour": player.game_hour,
                "game_minute": player.game_minute,
                "season": player.season,
                "total_game_days": player.total_game_days,
                "max_game_days": max_days,
            },
        })

    # 先用旧天数结算，确保当天所有事件都被包含在快照中
    if day_delta > 0:
        old_game_days = player.total_game_days
        season_id = get_current_season_id()
        settle_leaderboard_for_day(old_game_days, season_id)
        reset_player_action_points(player_id)

    # 再更新天数
    player.total_game_days += day_delta
    if player.total_game_days < 1:
        player.total_game_days = 1
    player.time_initialized = True

    player.game_year = new_date.year
    player.game_month = new_date.month
    player.game_day = new_date.day
    player.game_hour = new_hour
    player.game_minute = new_minute
    player.season = season
    player.save()

    if day_delta > 0:
        player.refresh_from_db()

    return Response({
        "success": True,
        "message": "day advanced",
        "data": {
            "game_year": player.game_year,
            "game_month": player.game_month,
            "game_day": player.game_day,
            "game_hour": player.game_hour,
            "game_minute": player.game_minute,
            "season": player.season,
            "total_game_days": player.total_game_days,
            "day_delta": day_delta,
            "action_points": player.action_points,
            "max_game_days": max_days,
        },
    })
