"""Season lifecycle: check, rotate, settle."""
import datetime
import logging
import time
import uuid

from django.db import transaction
from django.utils import timezone

from .models import Season, SeasonConfig, LeaderboardEntry, PendingReward, ReportEvent, BoardType, SettlementType, RewardConfig
from player.models import PlayerState, PlayerRecipe, PlayerTitle, DefaultRecipe
from account.models import GameAccount
from inventory.models import InventoryBag, InventorySlot, BagTemplate
from cooking.models import PityCounter
from game_time.models import GameTimeState
import json

logger = logging.getLogger(__name__)

# Game calendar initial values (matches PlayerState defaults and TimeManager.NewGameTime())
INITIAL_GAME_YEAR = 2026
INITIAL_GAME_MONTH = 3
INITIAL_GAME_DAY = 8
INITIAL_GAME_HOUR = 7
INITIAL_GAME_MINUTE = 0
INITIAL_SEASON = 0

# Season notice window: 03:45 ~ 04:00 (minutes since midnight)
_NOTICE_START_MINUTE = 3 * 60 + 45  # 225
_NOTICE_END_MINUTE = 4 * 60         # 240

# 赛季结束时间对齐：固定到次日 00:04（本地时区），恰好早于每日 00:05 的轮转调度。
_SEASON_END_ALIGN_HOUR = 0
_SEASON_END_ALIGN_MINUTE = 4


def aligned_season_end_time(start_ts, duration_days):
    """根据开始时间与时长计算结束时间，并向上对齐到 00:04（本地时区）。

    - 自然结束时刻（start + duration 天）若晚于当天 00:04，则顺延到次日 00:04；
    - 若自然结束恰好落在某天 00:04（如上一赛季已对齐时的链式轮转），则保持不变，
      保证多次轮转幂等、不会逐季向后漂移。
    """
    tz = timezone.get_current_timezone()
    natural_end = datetime.datetime.fromtimestamp(int(start_ts), tz=tz) + datetime.timedelta(days=duration_days)
    candidate = natural_end.replace(
        hour=_SEASON_END_ALIGN_HOUR,
        minute=_SEASON_END_ALIGN_MINUTE,
        second=0,
        microsecond=0,
    )
    if candidate < natural_end:
        candidate += datetime.timedelta(days=1)
    return int(candidate.timestamp())


def get_active_season():
    """Return the current active season. Creates the first season if none exists."""
    season = Season.objects.filter(status="active").order_by("-sequence").first()
    if season is None:
        config = SeasonConfig.get_instance()
        now = int(time.time())
        season = Season.objects.create(
            season_id="season_1",
            sequence=1,
            duration_days=config.default_duration_days,
            start_time=now,
            end_time=aligned_season_end_time(now, config.default_duration_days),
            status="active",
        )
        logger.info("[season] Created initial season: %s", season.season_id)
    return season


def check_and_rotate_season():
    """请求期安全网：若调度器漏跑 00:05，这里补建新赛季并把旧赛季标记 ended，
    但绝不直接重置玩家。

    结算与玩家迁移统一由 04:00 调度器 settle_and_transition_old_season() 或单人
    season-refresh -> transition_player_to_season() 完成，确保所有玩家都经过同一条
    重置路径（背包/菜谱/称号/保底计数器范围一致）。

    Returns the active Season (current or newly created).
    Idempotent: 赛季仍活跃时多次调用无副作用。
    """
    return rotate_season()


def settle_season(season):
    """Settle a season: snapshot final rankings and generate season rewards."""
    now_ts = int(time.time())
    expire_ts = now_ts + 86400 * 14  # Season rewards expire in 14 days
    season_id = season.season_id

    for board_type in BoardType.values:
        # Snapshot final rankings (use the DB-writing variant)
        from .services import calculate_rankings
        calculate_rankings(board_type, season_id, game_day=None)

        # Find matching season reward configs
        configs = RewardConfig.objects.filter(
            board_type=board_type,
            settlement_type=SettlementType.SEASON,
        )
        if not configs.exists():
            continue

        entries = LeaderboardEntry.objects.filter(
            board_type=board_type,
            season_id=season_id,
        )

        for entry in entries:
            reward_id = f"season_{board_type}_{season_id}_{entry.character_id}"

            if PendingReward.objects.filter(reward_id=reward_id).exists():
                continue

            rewards = []
            for config in configs:
                if config.rank_min <= entry.rank <= config.rank_max:
                    reward_data = config.reward_data
                    if isinstance(reward_data, str):
                        reward_data = json.loads(reward_data)
                    rewards.append({
                        "type": config.reward_type,
                        **reward_data,
                    })

            if rewards:
                PendingReward.objects.create(
                    player_id=entry.character_id,
                    reward_id=reward_id,
                    board_type=board_type,
                    settlement_type=SettlementType.SEASON,
                    rank=entry.rank,
                    rewards=rewards,
                    expire_time=expire_ts,
                )

    logger.info("[season] Settled season %s", season_id)


def is_player_season_reset(player):
    """Check if a player has been reset by a season rotation but the client hasn't synced yet."""
    return (
        player.total_game_days == 1
        and player.game_year == INITIAL_GAME_YEAR
        and player.game_month == INITIAL_GAME_MONTH
        and player.game_day == INITIAL_GAME_DAY
    )


def build_season_time_response(player, season):
    """Build the time snapshot response dict for day_advanced."""
    game_time_state = GameTimeState.get_instance()
    return {
        "game_year": player.game_year,
        "game_month": player.game_month,
        "game_day": player.game_day,
        "game_hour": player.game_hour,
        "game_minute": player.game_minute,
        "season": player.season,
        "total_game_days": player.total_game_days,
        "day_delta": 0,
        "action_points": player.action_points,
        "max_game_days": game_time_state.max_game_days,
        "seasonId": season.season_id,
        "seasonEndTime": season.end_time,
        "seasonSequence": season.sequence,
    }


# ───── New two-phase season rotation ─────


def rotate_season():
    """00:05 scheduler: create the next season without settling or resetting players.

    Marks the old season as 'ended' (not settled).
    Returns the new active Season.
    """
    current = get_active_season()
    now = int(time.time())

    if now < current.end_time:
        return current  # Still active

    config = SeasonConfig.get_instance()
    if not config.auto_rotate:
        return current

    logger.info("[rotate_season] Season %s expired, creating next season...", current.season_id)

    new_seq = current.sequence + 1
    new_season = Season.objects.create(
        season_id=f"season_{new_seq}",
        sequence=new_seq,
        duration_days=config.default_duration_days,
        start_time=current.end_time,
        end_time=aligned_season_end_time(current.end_time, config.default_duration_days),
        status="active",
    )

    # Mark old season as ended (NOT settled — settlement happens at 04:00)
    current.status = "ended"
    current.save(update_fields=["status"])

    logger.info("[rotate_season] Created %s, old season %s marked 'ended'", new_season.season_id, current.season_id)
    return new_season


def settle_and_transition_old_season():
    """04:00 scheduler: settle all ended-but-unsettled seasons, then force-transition remaining players.

    For each ended season:
      1. Settle it (snapshot rankings + generate rewards)
      2. Force-transition all players still on that season
    """
    ended_seasons = Season.objects.filter(status="ended", settled=False)

    for old_season in ended_seasons:
        logger.info("[settle] Settling old season %s...", old_season.season_id)

        # 1. Settle
        settle_season(old_season)
        old_season.status = "settled"
        old_season.settled = True
        old_season.save(update_fields=["status", "settled"])

        # 2. Force-transition remaining players
        force_transition_remaining_players(old_season.season_id)

        logger.info("[settle] Season %s settled, all remaining players transitioned", old_season.season_id)


def transition_player_to_season(player_state):
    """Transition a single player to the current active season.

    Resets game day, action points, inventory, recipes, titles, pity counters.
    Returns the new active season.
    """
    active_season = get_active_season()

    if player_state.current_season_id == active_season.season_id:
        return active_season  # Already on current season

    old_season_id = player_state.current_season_id
    logger.info("[transition] Player %s: %s -> %s", player_state.player_id, old_season_id, active_season.season_id)

    with transaction.atomic():
        # 1. Reset PlayerState time fields
        player_state.total_game_days = 1
        player_state.game_year = INITIAL_GAME_YEAR
        player_state.game_month = INITIAL_GAME_MONTH
        player_state.game_day = INITIAL_GAME_DAY
        player_state.game_hour = INITIAL_GAME_HOUR
        player_state.game_minute = INITIAL_GAME_MINUTE
        player_state.season = INITIAL_SEASON
        player_state.time_initialized = False
        player_state.action_points = 100
        player_state.current_season_id = active_season.season_id
        player_state.save()

        # 2. Reset inventory & money from templates
        try:
            account = GameAccount.objects.get(pk=int(player_state.player_id))
            _reset_player_inventory(account)
            _reset_player_money(account)
        except (GameAccount.DoesNotExist, ValueError):
            pass

        # 3. Reset recipes
        _reset_player_recipes(player_state)

        # 4. Clear titles
        player_state.titles.all().delete()

        # 5. Reset pity counters
        try:
            PityCounter.objects.filter(player_id=player_state.player_id).update(count_4=0, count_5=0, count_6=0)
        except Exception:
            pass

    logger.info("[transition] Player %s transitioned to %s", player_state.player_id, active_season.season_id)
    return active_season


def force_transition_remaining_players(old_season_id):
    """Force-transition all players whose current_season_id is still old_season_id."""
    players = PlayerState.objects.filter(current_season_id=old_season_id)
    count = players.count()
    if count == 0:
        logger.info("[force_transition] No players remaining on %s", old_season_id)
        return

    logger.info("[force_transition] Force-transitioning %d players from %s", count, old_season_id)
    for player_state in players:
        transition_player_to_season(player_state)


def get_season_notice(player_state):
    """Determine if a player should receive a season notice popup.

    Returns:
        "new_season_available" — 00:05 optional popup (dismissible)
        "season_refresh_required" — 03:45 mandatory popup (non-dismissible)
        None — no notice needed
    """
    active_season = get_active_season()

    # Player already on current season
    if player_state.current_season_id == active_season.season_id:
        return None

    # Check if we're in the 03:45-04:00 mandatory window
    now = timezone.localtime()
    minutes_since_midnight = now.hour * 60 + now.minute
    if _NOTICE_START_MINUTE <= minutes_since_midnight < _NOTICE_END_MINUTE:
        return "season_refresh_required"

    # Otherwise it's the optional notice (00:05-03:45 window)
    return "new_season_available"


def _reset_player_inventory(account):
    """Reset a player's inventory slots from BagTemplate."""
    bags = InventoryBag.objects.filter(player=account)
    InventorySlot.objects.filter(bag__in=bags).delete()

    templates = BagTemplate.objects.prefetch_related("slots").all()
    for tmpl in templates:
        cid = account.username if tmpl.character_id == "player" else tmpl.character_id
        bag = bags.filter(character_id=cid).first()
        if not bag:
            continue
        for slot in tmpl.slots.select_related("item").all():
            InventorySlot.objects.create(
                bag=bag,
                slot_index=slot.slot_index,
                instance_id=uuid.uuid4(),
                item_id=slot.item.item_id if slot.item else 0,
                item_amount=slot.item_amount,
                rated=slot.rated,
                rating_price=slot.rating_price,
                overall_score=slot.overall_score,
                ingredient=slot.ingredient,
                item_bag_name=slot.item.item_name if slot.item else "",
                cook_time=slot.cook_time,
                dish_quality=slot.dish_quality,
            )


def _reset_player_money(account):
    """Reset a player's bag money from templates."""
    templates = {t.character_id: t.money for t in BagTemplate.objects.all()}
    bags = InventoryBag.objects.filter(player=account)
    for bag in bags:
        cid = "player" if bag.character_id == account.username else bag.character_id
        bag.money = templates.get(cid, 0)
        bag.save(update_fields=["money"])


def _reset_player_recipes(player_state):
    """Reset a player's recipes from DefaultRecipe."""
    player_state.recipes.all().delete()
    for dr in DefaultRecipe.objects.all():
        from recipes.models import Recipe
        catalog = Recipe.objects.filter(recipe_name=dr.recipe_name).first()
        PlayerRecipe.objects.create(
            player=player_state,
            recipe_name=dr.recipe_name,
            catalog_recipe=catalog,
        )


def full_reset_to_season_1():
    """全量重置回第一赛季：清除所有赛季/排行榜/玩家数据，回到初始状态。

    Returns:
        dict: 各项重置的统计数字。
    """
    stats = {}

    with transaction.atomic():
        # ── 1. 排行榜 & 赛季数据 ──
        stats["seasons_deleted"] = Season.objects.all().delete()[0]
        stats["leaderboard_entries_deleted"] = LeaderboardEntry.objects.all().delete()[0]
        stats["report_events_deleted"] = ReportEvent.objects.all().delete()[0]
        stats["pending_rewards_deleted"] = PendingReward.objects.all().delete()[0]

        # ── 2. 创建新的 season_1 ──
        config = SeasonConfig.get_instance()
        now = int(time.time())
        Season.objects.create(
            season_id="season_1",
            sequence=1,
            duration_days=config.default_duration_days,
            start_time=now,
            end_time=aligned_season_end_time(now, config.default_duration_days),
            status="active",
        )
        stats["new_season"] = "season_1"

        # ── 3. 重置所有 PlayerState ──
        stats["players_reset"] = PlayerState.objects.all().update(
            total_game_days=1,
            game_year=INITIAL_GAME_YEAR,
            game_month=INITIAL_GAME_MONTH,
            game_day=INITIAL_GAME_DAY,
            game_hour=INITIAL_GAME_HOUR,
            game_minute=INITIAL_GAME_MINUTE,
            season=INITIAL_SEASON,
            time_initialized=False,
            action_points=100,
        )

        # ── 4. 重置所有玩家背包 & 金钱 ──
        accounts = GameAccount.objects.all()
        templates = {t.character_id: t for t in BagTemplate.objects.prefetch_related("slots").all()}
        player_tmpl = templates.get("player")

        stats["inventory_bags_reset"] = 0
        stats["inventory_slots_deleted"] = 0
        stats["inventory_slots_created"] = 0

        for account in accounts:
            bags = InventoryBag.objects.filter(player=account)
            deleted = InventorySlot.objects.filter(bag__in=bags).delete()[0]
            stats["inventory_slots_deleted"] += deleted

            for bag in bags:
                cid = "player" if bag.character_id == account.username else bag.character_id
                tmpl = templates.get(cid)
                if not tmpl:
                    continue
                bag.money = tmpl.money
                bag.save(update_fields=["money"])
                stats["inventory_bags_reset"] += 1

                for slot in tmpl.slots.select_related("item").all():
                    InventorySlot.objects.create(
                        bag=bag,
                        slot_index=slot.slot_index,
                        instance_id=uuid.uuid4(),
                        item_id=slot.item.item_id if slot.item else 0,
                        item_amount=slot.item_amount,
                        rated=slot.rated,
                        rating_price=slot.rating_price,
                        overall_score=slot.overall_score,
                        ingredient=slot.ingredient,
                        item_bag_name=slot.item.item_name if slot.item else "",
                        cook_time=slot.cook_time,
                        dish_quality=slot.dish_quality,
                    )
                    stats["inventory_slots_created"] += 1

        # ── 5. 重置所有玩家食谱 ──
        stats["recipes_deleted"] = PlayerRecipe.objects.all().delete()[0]
        defaults = list(DefaultRecipe.objects.all())
        stats["recipes_created"] = 0
        for ps in PlayerState.objects.all():
            for dr in defaults:
                from recipes.models import Recipe
                catalog = Recipe.objects.filter(recipe_name=dr.recipe_name).first()
                PlayerRecipe.objects.create(
                    player=ps,
                    recipe_name=dr.recipe_name,
                    catalog_recipe=catalog,
                )
                stats["recipes_created"] += 1

        # ── 6. 清除所有称号 ──
        stats["titles_deleted"] = PlayerTitle.objects.all().delete()[0]

        # ── 7. 重置所有烹饪保底计数器 ──
        stats["pity_counters_reset"] = PityCounter.objects.all().update(count_4=0, count_5=0, count_6=0)

        # ── 8. 重置全局游戏时间 ──
        game_time = GameTimeState.get_instance()
        game_time.game_day = 1
        game_time.game_year = INITIAL_GAME_YEAR
        game_time.game_month = INITIAL_GAME_MONTH
        game_time.game_hour = INITIAL_GAME_HOUR
        game_time.game_minute = INITIAL_GAME_MINUTE
        game_time.season = INITIAL_SEASON
        game_time.save()
        stats["game_time_reset"] = True

    logger.info("[full_reset] All data reset to season_1: %s", stats)
    return stats
