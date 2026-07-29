import datetime
import logging
import time

from django.db import transaction
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from config.auth import get_player_id
from account.models import GameAccount
from player.models import PlayerState
from inventory.models import InventoryBag
from inventory.services import restock_npc_bags
from leaderboard.services import settle_leaderboard_for_day, settle_milestone_for_player
from season.season_service import (
    check_and_rotate_season,
    get_active_season,
    get_season_notice,
    transition_player_to_season,
    build_season_time_response,
)
from season.models import SeasonConfig
from account.env_auth import EnvServerJWTAuthentication


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
def time_sync(request):
    """轻量级时间同步：仅更新客户端的小时/分钟，不触发赛季检查、排行榜结算等重逻辑。"""
    player_id = get_player_id(request)
    player, _ = PlayerState.objects.get_or_create(player_id=player_id)

    player.game_hour = int(request.data.get("game_hour", player.game_hour))
    player.game_minute = int(request.data.get("game_minute", player.game_minute))
    player.save(update_fields=["game_hour", "game_minute"])

    return Response({"success": True})


@api_view(["POST"])
def check_day_advance(request):
    """预检查是否允许推进下一天，不修改任何数据。"""
    player_id = get_player_id(request)
    player, _ = PlayerState.objects.get_or_create(player_id=player_id)

    # Season check (creates initial season if needed)
    current_season = check_and_rotate_season()

    # Ensure player has current_season_id set
    if not player.current_season_id:
        player.current_season_id = current_season.season_id
        player.save(update_fields=["current_season_id"])

    max_days = SeasonConfig.get_instance().max_game_days

    if max_days > 0 and player.total_game_days >= max_days:
        return Response({
            "success": True,
            "message": "last_day",
            "data": {
                "seasonId": current_season.season_id,
                "seasonEndTime": current_season.end_time,
                "isLastDay": True,
            },
        })

    data = {
        "seasonId": current_season.season_id,
        "seasonEndTime": current_season.end_time,
    }

    # Season notice
    notice = get_season_notice(player)
    if notice:
        data["seasonNotice"] = notice

    return Response({
        "success": True,
        "message": "ok",
        "data": data,
    })


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

    # Season check: may trigger rotation and reset all players
    current_season = check_and_rotate_season()

    new_year = int(request.data.get("game_year", 2026))
    new_month = int(request.data.get("game_month", 3))
    new_day = int(request.data.get("game_day", 8))
    new_hour = int(request.data.get("game_hour", 7))
    new_minute = int(request.data.get("game_minute", 0))
    season = int(request.data.get("season", 0))

    new_date = _safe_date(new_year, new_month, new_day)

    player, _ = PlayerState.objects.get_or_create(player_id=player_id)

    # Ensure player has current_season_id set
    if not player.current_season_id:
        player.current_season_id = current_season.season_id
        player.save(update_fields=["current_season_id"])

    # 全服最大天数限制（仅阻止实际天数推进，不影响时间同步）
    max_days = SeasonConfig.get_instance().max_game_days

    # 模型默认值（2026-3-8, total_game_days=1）即作为新玩家起点：
    # - 客户端首次以 3-8 上报 → delta=0（同日，幂等），仍是第 1 天；
    # - 客户端推进到 3-9 上报 → delta=1，进入第 2 天。
    old_date = _safe_date(player.game_year, player.game_month, player.game_day)
    raw_delta = (new_date - old_date).days

    # 先同步客户端日历到服务端，确保不因日历脱节导致请求被拒
    player.game_year = new_date.year
    player.game_month = new_date.month
    player.game_day = new_date.day
    player.game_hour = new_hour
    player.game_minute = new_minute

    day_delta = 1 if raw_delta > 0 else 0

    # 最后一天：结算当天排行榜，但不推进天数
    if day_delta > 0 and max_days > 0 and player.total_game_days >= max_days:
        old_game_days = player.total_game_days
        season_id = current_season.season_id
        settle_leaderboard_for_day(old_game_days, season_id, player_id=player_id)

        return Response({
            "success": True,
            "message": "last_day_settled",
            "data": {
                "game_year": player.game_year,
                "game_month": player.game_month,
                "game_day": player.game_day,
                "game_hour": player.game_hour,
                "game_minute": player.game_minute,
                "season": player.season,
                "total_game_days": player.total_game_days,
                "day_delta": 0,
                "action_points": player.action_points,
                "max_game_days": max_days,
                "isLastDay": True,
            },
        })

    # 先用旧天数结算，确保当天所有事件都被包含在快照中
    if day_delta > 0:
        old_game_days = player.total_game_days
        season_id = current_season.season_id
        settle_leaderboard_for_day(old_game_days, season_id, player_id=player_id)
        player.action_points = 100
        restock_npc_bags(request.user)

        # 保存新一天的金币快照（用于富豪榜日榜）和总榜快照
        try:
            account = GameAccount.objects.get(id=player.player_id)
            bag = InventoryBag.objects.get(
                player_id=player.player_id,
                character_id=account.username,
            )
            player.total_wealth_snapshot = bag.money
            player.day_start_money = bag.money

            # 记录结算时金币快照（永久保留，用于历史重建）
            from leaderboard.models import ReportEvent
            ReportEvent.objects.create(
                player_id=player.player_id,
                event_type="day_settlement",
                payload={"balance": bag.money},
                timestamp=int(time.time()),
                game_day=old_game_days,
                season_id=season_id,
            )
        except InventoryBag.DoesNotExist:
            player.total_wealth_snapshot = 0
            player.day_start_money = 0
        player.day_start_money_game_day = player.total_game_days + 1

    # 再更新天数（日历已在前面同步）
    player.total_game_days += day_delta
    if player.total_game_days < 1:
        player.total_game_days = 1
    player.time_initialized = True
    player.season = season
    player.save()

    if day_delta > 0:
        player.refresh_from_db()

        # 不是最后一天时，为新一天创建榜单条目
        if max_days <= 0 or player.total_game_days < max_days:
            from leaderboard.services import refresh_leaderboard
            refresh_leaderboard(season_id=current_season.season_id, game_day=player.total_game_days)

        # 里程碑结算：每满 N 天根据总榜发放奖励
        milestone_days = SeasonConfig.get_instance().milestone_days
        if milestone_days > 0 and player.total_game_days % milestone_days == 0:
            settle_milestone_for_player(player_id, player.total_game_days, current_season.season_id)

    data = {
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
        "seasonId": current_season.season_id,
        "seasonEndTime": current_season.end_time,
        "seasonSequence": current_season.sequence,
    }

    # Season notice (check on every minute broadcast)
    notice = get_season_notice(player)
    if notice:
        data["seasonNotice"] = notice

    return Response({
        "success": True,
        "message": "day advanced",
        "data": data,
    })


@api_view(["GET"])
def game_init(request):
    """登录后一次性返回玩家时间状态 + 赛季元数据，避免多次请求。"""
    player_id = get_player_id(request)
    player, _ = PlayerState.objects.get_or_create(player_id=player_id)

    current_season = check_and_rotate_season()

    # Ensure player has current_season_id set
    if not player.current_season_id:
        player.current_season_id = current_season.season_id
        player.save(update_fields=["current_season_id"])

    # Check season notice
    notice = get_season_notice(player)

    data = {
        "game_year": player.game_year,
        "game_month": player.game_month,
        "game_day": player.game_day,
        "game_hour": player.game_hour,
        "game_minute": player.game_minute,
        "season": player.season,
        "total_game_days": player.total_game_days,
        "time_initialized": player.time_initialized,
        "action_points": player.action_points,
        "action_points_max": player.action_points_max,
        "max_game_days": SeasonConfig.get_instance().max_game_days,
        "seasonId": current_season.season_id,
        "seasonSequence": current_season.sequence,
        "seasonEndTime": current_season.end_time,
    }

    if notice:
        data["seasonNotice"] = notice

    return Response({
        "code": 0,
        "data": data,
    })


@api_view(["POST"])
def season_refresh(request):
    """Player confirms season transition. Transitions the player to the current active season."""
    player_id = get_player_id(request)
    player, _ = PlayerState.objects.get_or_create(player_id=player_id)

    new_season = transition_player_to_season(player)
    player.refresh_from_db()

    return Response({
        "success": True,
        "message": "season refreshed",
        "data": build_season_time_response(player, new_season),
    })


# ─────────────── 全局游戏时间 API（Env Server专用） ───────────────

from .models import GlobalGameTimeState


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def global_time_get(request):
    """获取全服统一游戏时间。Env Server启动时调用。"""
    state = GlobalGameTimeState.get_instance()
    return Response({
        "success": True,
        "data": {
            "game_year": state.game_year,
            "game_month": state.game_month,
            "game_day": state.game_day,
            "game_hour": state.game_hour,
            "game_minute": state.game_minute,
            "season": state.season,
            "total_game_days": state.total_game_days,
            "current_season_id": state.current_season_id,
            "max_game_days": SeasonConfig.get_instance().max_game_days,
        }
    })


@api_view(["PUT"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def global_time_update(request):
    """更新全服统一游戏时间。Env Server推进时间后调用。

    请求体: {"game_year": 2026, "game_month": 3, "game_day": 9, "game_hour": 8, "game_minute": 30, "season": 0, "total_game_days": 2, "current_season_id": "s1"}
    """
    state = GlobalGameTimeState.get_instance()

    for field in ["game_year", "game_month", "game_day", "game_hour", "game_minute", "season", "total_game_days", "current_season_id"]:
        val = request.data.get(field)
        if val is not None:
            setattr(state, field, val)

    state.save()

    return Response({
        "success": True,
        "data": {
            "game_year": state.game_year,
            "game_month": state.game_month,
            "game_day": state.game_day,
            "game_hour": state.game_hour,
            "game_minute": state.game_minute,
            "season": state.season,
            "total_game_days": state.total_game_days,
            "current_season_id": state.current_season_id,
        }
    })


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
@transaction.atomic
def global_time_day_advanced(request):
    """全局日推进（Env Server专用）。推进天数、结算排行榜、补充NPC库存。

    请求体: {"game_year": 2026, "game_month": 3, "game_day": 9, "game_hour": 7, "game_minute": 0, "season": 0}
    """
    from inventory.services import restock_npc_bags_global
    from leaderboard.services import refresh_leaderboard

    # Lock the singleton row so two Env Server processes cannot both settle the
    # same game day or restock the same shared inventory.
    GlobalGameTimeState.get_instance()
    state = GlobalGameTimeState.objects.select_for_update().get(pk=1)
    current_season = check_and_rotate_season()

    new_year = int(request.data.get("game_year", state.game_year))
    new_month = int(request.data.get("game_month", state.game_month))
    new_day = int(request.data.get("game_day", state.game_day))
    new_hour = int(request.data.get("game_hour", state.game_hour))
    new_minute = int(request.data.get("game_minute", state.game_minute))
    season = int(request.data.get("season", state.season))

    new_date = _safe_date(new_year, new_month, new_day)
    old_date = _safe_date(state.game_year, state.game_month, state.game_day)
    raw_delta = (new_date - old_date).days

    max_days = SeasonConfig.get_instance().max_game_days
    if raw_delta < 0:
        return Response({
            "success": True,
            "message": "stale time, ignored",
            "data": {
                "game_year": state.game_year, "game_month": state.game_month, "game_day": state.game_day,
                "game_hour": state.game_hour, "game_minute": state.game_minute, "season": state.season,
                "total_game_days": state.total_game_days, "max_game_days": max_days,
            },
        })
    if raw_delta > 0 and max_days > 0 and state.total_game_days >= max_days:
        return Response({
            "success": False,
            "message": "已达到赛季最大天数",
            "data": {
                "game_year": state.game_year, "game_month": state.game_month, "game_day": state.game_day,
                "game_hour": state.game_hour, "game_minute": state.game_minute, "season": state.season,
                "total_game_days": state.total_game_days, "max_game_days": max_days,
            },
        })

    state.game_year = new_date.year
    state.game_month = new_date.month
    state.game_day = new_date.day
    state.game_hour = new_hour
    state.game_minute = new_minute
    state.season = season

    day_delta = 1 if raw_delta > 0 else 0
    state.total_game_days += day_delta
    if state.total_game_days < 1:
        state.total_game_days = 1
    state.current_season_id = current_season.season_id
    state.save()

    if day_delta > 0:
        # 补充全局NPC库存
        try:
            restock_npc_bags_global()
        except Exception:
            logger.warning("restock_npc_bags_global failed", exc_info=True)

        # 刷新排行榜
        try:
            refresh_leaderboard(season_id=current_season.season_id, game_day=state.total_game_days)
        except Exception:
            logger.warning("refresh_leaderboard failed", exc_info=True)

    data = {
        "game_year": state.game_year,
        "game_month": state.game_month,
        "game_day": state.game_day,
        "game_hour": state.game_hour,
        "game_minute": state.game_minute,
        "season": state.season,
        "total_game_days": state.total_game_days,
        "day_delta": day_delta,
        "max_game_days": max_days,
        "seasonId": current_season.season_id,
        "seasonEndTime": current_season.end_time,
    }

    return Response({
        "success": True,
        "message": "day advanced",
        "data": data,
    })
