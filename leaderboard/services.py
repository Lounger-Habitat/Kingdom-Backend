"""Business logic for leaderboard ranking, daily/weekly settlement."""
import json
import random
import time
from collections import defaultdict
from django.db.models import Sum, Max, Count, F
from .models import (
    LeaderboardEntry, RewardConfig, PendingReward, ReportEvent, BoardType, SettlementType,
)
from season.models import SeasonConfig
from player.models import PlayerState, PlayerTitle
from account.models import GameAccount
from inventory.models import InventoryBag


def calculate_rankings_realtime(board_type, player_id=None, game_day=None, season_id=None):
    """Real-time ranking calculation from ReportEvent, without writing to DB.

    Returns (entries_list, my_rank_dict):
      - entries_list: list of dicts with rank, characterId, displayName, isAI, score, title, avatarId
      - my_rank_dict: dict with rank, score for the given player_id, or None
    """
    if board_type == BoardType.WEALTH:
        # 富豪榜日榜：使用 day_settlement 事件计算当日财富变化
        scores = {}

        if game_day is not None and game_day > 1:
            prev_day = game_day - 1
            prev_settlements = ReportEvent.objects.filter(
                event_type="day_settlement", game_day=prev_day
            )
            prev_balance = {}
            for ev in prev_settlements:
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                prev_balance[ev.player_id] = payload.get("balance", 0)

            curr_settlements = ReportEvent.objects.filter(
                event_type="day_settlement", game_day=game_day
            )
            curr_balance = {}
            for ev in curr_settlements:
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                curr_balance[ev.player_id] = payload.get("balance", 0)

            if curr_balance:
                # 历史日：优先用两天结算数据差值；缺失前一天数据时回退到 day_start_money
                for pid, curr in curr_balance.items():
                    if pid in prev_balance:
                        scores[pid] = curr - prev_balance[pid]
                    else:
                        try:
                            ps = PlayerState.objects.get(player_id=pid)
                            if ps.day_start_money_game_day == game_day:
                                scores[pid] = curr - ps.day_start_money
                        except PlayerState.DoesNotExist:
                            pass
                # 有 prev_balance 但无 curr_balance 的玩家：用当前背包计算
                for pid, prev in prev_balance.items():
                    if pid not in scores:
                        try:
                            account = GameAccount.objects.get(id=pid)
                            bag = InventoryBag.objects.get(player_id=pid, character_id=account.username)
                            scores[pid] = bag.money - prev
                        except (GameAccount.DoesNotExist, InventoryBag.DoesNotExist):
                            continue
            elif prev_balance:
                # 当日（尚未结算）：用当前背包金额 - 上一日结算快照
                for pid, prev in prev_balance.items():
                    try:
                        account = GameAccount.objects.get(id=pid)
                        bag = InventoryBag.objects.get(player_id=pid, character_id=account.username)
                        scores[pid] = bag.money - prev
                    except (GameAccount.DoesNotExist, InventoryBag.DoesNotExist):
                        continue
            else:
                # 无任何结算数据：回退到 day_start_money
                for ps in PlayerState.objects.filter(day_start_money_game_day=game_day):
                    try:
                        account = GameAccount.objects.get(id=ps.player_id)
                        bag = InventoryBag.objects.get(player_id=ps.player_id, character_id=account.username)
                        scores[ps.player_id] = bag.money - ps.day_start_money
                    except (GameAccount.DoesNotExist, InventoryBag.DoesNotExist):
                        continue

        elif game_day == 1:
            from inventory.models import BagTemplate
            try:
                player_tmpl = BagTemplate.objects.get(character_id="player")
                initial_money = player_tmpl.money
            except BagTemplate.DoesNotExist:
                initial_money = 0

            settlements = ReportEvent.objects.filter(event_type="day_settlement", game_day=1)
            settlement_balance = {}
            for ev in settlements:
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                settlement_balance[ev.player_id] = payload.get("balance", 0)

            # 用 BagTemplate 作为所有玩家的基准，再用 settlement 数据覆盖
            for bag in InventoryBag.objects.filter(player__isnull=False, character_id=F("player__username")).values("player_id", "money"):
                pid = str(bag["player_id"])
                if pid in settlement_balance:
                    scores[pid] = settlement_balance[pid] - initial_money
                else:
                    scores[pid] = bag["money"] - initial_money

    elif board_type == BoardType.OUTPUT:
        qs = ReportEvent.objects.filter(event_type="cook_completed")
        if season_id:
            qs = qs.filter(season_id=season_id)
        if game_day is not None:
            qs = qs.filter(game_day=game_day)
        counts = qs.values("player_id").annotate(count=Count("id"))
        scores = {c["player_id"]: c["count"] for c in counts}

    elif board_type == BoardType.FRESHNESS:
        top_n = SeasonConfig.get_instance().freshness_top_n
        qs = ReportEvent.objects.filter(event_type="judge_confirmed")
        if season_id:
            qs = qs.filter(season_id=season_id)
        if game_day is not None:
            qs = qs.filter(game_day=game_day)
        events = qs.values("player_id", "payload")
        player_dishes = defaultdict(list)
        for ev in events:
            try:
                payload = ev.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload)
                pid = ev["player_id"]
                score = payload.get("judge_score", 0)
                dish = payload.get("dish_name", "")
                dishes = player_dishes[pid]
                if len(dishes) < top_n:
                    dishes.append((score, dish))
                    dishes.sort(reverse=True)
                elif score > dishes[-1][0]:
                    dishes[-1] = (score, dish)
                    dishes.sort(reverse=True)
            except (json.JSONDecodeError, TypeError):
                continue

    elif board_type == BoardType.TOTAL_WEALTH:
        scores = {}
        for bag in InventoryBag.objects.filter(
            player__isnull=False, character_id=F("player__username")
        ).values("player_id", "money"):
            pid = str(bag["player_id"])
            scores[pid] = scores.get(pid, 0) + bag["money"]

    elif board_type == BoardType.TOTAL_OUTPUT:
        qs = ReportEvent.objects.filter(event_type="cook_completed")
        if season_id:
            qs = qs.filter(season_id=season_id)
        counts = qs.values("player_id").annotate(count=Count("id"))
        scores = {c["player_id"]: c["count"] for c in counts}

    elif board_type == BoardType.TOTAL_FRESHNESS:
        top_n = SeasonConfig.get_instance().freshness_top_n
        qs = ReportEvent.objects.filter(event_type="judge_confirmed")
        if season_id:
            qs = qs.filter(season_id=season_id)
        events = qs.values("player_id", "payload")
        player_dishes = defaultdict(list)
        for ev in events:
            try:
                payload = ev.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload)
                pid = ev["player_id"]
                score = payload.get("judge_score", 0)
                dish = payload.get("dish_name", "")
                dishes = player_dishes[pid]
                if len(dishes) < top_n:
                    dishes.append((score, dish))
                    dishes.sort(reverse=True)
                elif score > dishes[-1][0]:
                    dishes[-1] = (score, dish)
                    dishes.sort(reverse=True)
            except (json.JSONDecodeError, TypeError):
                continue
    else:
        return [], None

    is_freshness = board_type in (BoardType.FRESHNESS, BoardType.TOTAL_FRESHNESS)
    if is_freshness:
        all_items = []
        for pid, dishes in player_dishes.items():
            for score, dish in dishes:
                all_items.append((pid, score, dish))
        ranked = sorted(all_items, key=lambda x: x[1], reverse=True)
    else:
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # Batch fetch player names, AI flags, and titles to avoid N+1
    player_ids = list({pid for pid, *_ in ranked})
    usernames = {}
    ai_flags = {}
    for acc in GameAccount.objects.filter(id__in=player_ids):
        usernames[str(acc.id)] = acc.username
        ai_flags[str(acc.id)] = acc.is_ai

    active_titles = {}
    for pt in PlayerTitle.objects.filter(player_id__in=player_ids, is_active=True):
        active_titles[pt.player_id] = pt.title_name

    entries = []
    my_rank = None
    for rank, item in enumerate(ranked, start=1):
        if is_freshness:
            pid, score, dish_name = item
        else:
            pid, score = item
            dish_name = ""
        entry = {
            "rank": rank,
            "characterId": pid,
            "displayName": usernames.get(pid, pid),
            "isAI": ai_flags.get(pid, False),
            "score": score,
            "title": active_titles.get(pid, ""),
            "avatarId": f"avatar_{pid}",
            "dishName": dish_name,
        }
        entries.append(entry)
        if pid == player_id and my_rank is None:
            my_rank = {"rank": rank, "score": score}

    return entries, my_rank


def calculate_rankings(board_type, season_id="default", game_day=None):
    """Calculate rankings for a given board type based on ReportEvents."""

    if board_type == BoardType.WEALTH:
        # 富豪榜日榜：使用 day_settlement 事件计算当日财富变化
        scores = {}

        if game_day is not None and game_day > 1:
            prev_day = game_day - 1
            prev_settlements = ReportEvent.objects.filter(
                event_type="day_settlement", game_day=prev_day
            )
            prev_balance = {}
            for ev in prev_settlements:
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                prev_balance[ev.player_id] = payload.get("balance", 0)

            curr_settlements = ReportEvent.objects.filter(
                event_type="day_settlement", game_day=game_day
            )
            curr_balance = {}
            for ev in curr_settlements:
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                curr_balance[ev.player_id] = payload.get("balance", 0)

            if curr_balance:
                # 历史日：优先用两天结算数据差值；缺失前一天数据时回退到 day_start_money
                for pid, curr in curr_balance.items():
                    if pid in prev_balance:
                        scores[pid] = curr - prev_balance[pid]
                    else:
                        try:
                            ps = PlayerState.objects.get(player_id=pid)
                            if ps.day_start_money_game_day == game_day:
                                scores[pid] = curr - ps.day_start_money
                        except PlayerState.DoesNotExist:
                            pass
                # 有 prev_balance 但无 curr_balance 的玩家：用当前背包计算
                for pid, prev in prev_balance.items():
                    if pid not in scores:
                        try:
                            account = GameAccount.objects.get(id=pid)
                            bag = InventoryBag.objects.get(player_id=pid, character_id=account.username)
                            scores[pid] = bag.money - prev
                        except (GameAccount.DoesNotExist, InventoryBag.DoesNotExist):
                            continue
            elif prev_balance:
                # 当日（尚未结算）：用当前背包金额 - 上一日结算快照
                for pid, prev in prev_balance.items():
                    try:
                        account = GameAccount.objects.get(id=pid)
                        bag = InventoryBag.objects.get(player_id=pid, character_id=account.username)
                        scores[pid] = bag.money - prev
                    except (GameAccount.DoesNotExist, InventoryBag.DoesNotExist):
                        continue
            else:
                # 无任何结算数据：回退到 day_start_money
                for ps in PlayerState.objects.filter(day_start_money_game_day=game_day):
                    try:
                        account = GameAccount.objects.get(id=ps.player_id)
                        bag = InventoryBag.objects.get(player_id=ps.player_id, character_id=account.username)
                        scores[ps.player_id] = bag.money - ps.day_start_money
                    except (GameAccount.DoesNotExist, InventoryBag.DoesNotExist):
                        continue

        elif game_day == 1:
            from inventory.models import BagTemplate
            try:
                player_tmpl = BagTemplate.objects.get(character_id="player")
                initial_money = player_tmpl.money
            except BagTemplate.DoesNotExist:
                initial_money = 0

            settlements = ReportEvent.objects.filter(event_type="day_settlement", game_day=1)
            settlement_balance = {}
            for ev in settlements:
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                settlement_balance[ev.player_id] = payload.get("balance", 0)

            # 用 BagTemplate 作为所有玩家的基准，再用 settlement 数据覆盖
            for bag in InventoryBag.objects.filter(player__isnull=False, character_id=F("player__username")).values("player_id", "money"):
                pid = str(bag["player_id"])
                if pid in settlement_balance:
                    scores[pid] = settlement_balance[pid] - initial_money
                else:
                    scores[pid] = bag["money"] - initial_money

    elif board_type == BoardType.OUTPUT:
        qs = ReportEvent.objects.filter(event_type="cook_completed", season_id=season_id)
        if game_day is not None:
            qs = qs.filter(game_day=game_day)
        counts = qs.values("player_id").annotate(count=Count("id"))
        scores = {c["player_id"]: c["count"] for c in counts}

    elif board_type == BoardType.FRESHNESS:
        top_n = SeasonConfig.get_instance().freshness_top_n
        qs = ReportEvent.objects.filter(event_type="judge_confirmed", season_id=season_id)
        if game_day is not None:
            qs = qs.filter(game_day=game_day)
        events = qs.values("player_id", "payload")
        player_dishes = defaultdict(list)
        for ev in events:
            try:
                payload = ev.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload)
                pid = ev["player_id"]
                score = payload.get("judge_score", 0)
                dish = payload.get("dish_name", "")
                dishes = player_dishes[pid]
                if len(dishes) < top_n:
                    dishes.append((score, dish))
                    dishes.sort(reverse=True)
                elif score > dishes[-1][0]:
                    dishes[-1] = (score, dish)
                    dishes.sort(reverse=True)
            except (json.JSONDecodeError, TypeError):
                continue

    elif board_type == BoardType.TOTAL_WEALTH:
        scores = {}
        bag_qs = InventoryBag.objects.filter(
            player__isnull=False, character_id=F("player__username")
        )
        for bag in bag_qs.values("player_id", "money"):
            pid = str(bag["player_id"])
            scores[pid] = scores.get(pid, 0) + bag["money"]

    elif board_type == BoardType.TOTAL_OUTPUT:
        qs = ReportEvent.objects.filter(event_type="cook_completed")
        if season_id:
            qs = qs.filter(season_id=season_id)
        if game_day is not None:
            qs = qs.filter(game_day__lte=game_day)
        counts = qs.values("player_id").annotate(count=Count("id"))
        scores = {c["player_id"]: c["count"] for c in counts}

    elif board_type == BoardType.TOTAL_FRESHNESS:
        top_n = SeasonConfig.get_instance().freshness_top_n
        qs = ReportEvent.objects.filter(event_type="judge_confirmed")
        if season_id:
            qs = qs.filter(season_id=season_id)
        if game_day is not None:
            qs = qs.filter(game_day__lte=game_day)
        events = qs.values("player_id", "payload")
        player_dishes = defaultdict(list)
        for ev in events:
            try:
                payload = ev.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload)
                pid = ev["player_id"]
                score = payload.get("judge_score", 0)
                dish = payload.get("dish_name", "")
                dishes = player_dishes[pid]
                if len(dishes) < top_n:
                    dishes.append((score, dish))
                    dishes.sort(reverse=True)
                elif score > dishes[-1][0]:
                    dishes[-1] = (score, dish)
                    dishes.sort(reverse=True)
            except (json.JSONDecodeError, TypeError):
                continue
    else:
        return

    # For WEALTH, only delete old entries if we have new data (valid snapshots).
    # This preserves historical entries when snapshots are lost after day advance.
    if board_type == BoardType.WEALTH and game_day is not None and not scores:
        return

    # Clear existing entries for this board type + season to avoid duplicates
    is_total = board_type.startswith("total_")
    entry_game_day = None if is_total else game_day
    qs = LeaderboardEntry.objects.filter(board_type=board_type, season_id=season_id)
    if entry_game_day is not None:
        qs = qs.filter(game_day=entry_game_day)
    else:
        qs = qs.filter(game_day__isnull=True)
    qs.delete()

    # Build ranked list: freshness boards use (pid, score, dish_name) tuples
    is_freshness = board_type in (BoardType.FRESHNESS, BoardType.TOTAL_FRESHNESS)
    if is_freshness:
        all_items = []
        for pid, dishes in player_dishes.items():
            for score, dish in dishes:
                all_items.append((pid, score, dish))
        ranked = sorted(all_items, key=lambda x: x[1], reverse=True)
    else:
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # Batch fetch usernames and AI flags
    player_ids = list({pid for pid, *_ in ranked})
    usernames = {}
    ai_flags = {}
    for acc in GameAccount.objects.filter(id__in=player_ids):
        usernames[str(acc.id)] = acc.username
        ai_flags[str(acc.id)] = acc.is_ai

    entries = []
    for rank, item in enumerate(ranked, start=1):
        if is_freshness:
            player_id, score, dish_name = item
        else:
            player_id, score = item
            dish_name = ""
        display_name = usernames.get(player_id, player_id)

        # Get active title
        title = ""
        try:
            active_title = PlayerTitle.objects.filter(player_id=player_id, is_active=True).first()
            if active_title:
                title = active_title.title_name
        except Exception:
            pass

        entries.append(LeaderboardEntry(
            board_type=board_type,
            season_id=season_id,
            character_id=player_id,
            display_name=display_name,
            is_ai=ai_flags.get(player_id, False),
            score=score,
            rank=rank,
            title=title,
            avatar_id=f"avatar_{player_id}",
            dish_name=dish_name,
            game_day=entry_game_day,
        ))

    LeaderboardEntry.objects.bulk_create(entries)


def refresh_leaderboard(board_type=None, season_id="default", game_day=None):
    """Refresh LeaderboardEntry for the given board type(s).

    Call after any event report or money change to keep entries up-to-date.
    If board_type is None, refreshes all board types.
    """
    if board_type is not None:
        calculate_rankings(board_type, season_id, game_day=game_day)
    else:
        for bt in BoardType.values:
            calculate_rankings(bt, season_id, game_day=game_day)


def settle_leaderboard_for_day(total_game_days, season_id="default", player_id=None):
    """Recalculate rankings and generate rewards for a given cumulative game day.

    `total_game_days` is the player/global cumulative day count ("第 N 天")，
    保证全局唯一，避免月末 day-of-month 翻转导致 reward_id 冲突。
    `player_id` — if provided, only create PendingReward for this player (others'
    rewards will be settled when they advance their own day).
    Idempotent: PendingReward's unique reward_id prevents duplicates."""
    now_ts = int(time.time())
    expire_ts = now_ts + 86400 * 7  # Rewards expire in 7 days

    for board_type in BoardType.values:
        # Calculate current rankings for this specific day (global, so ranks are correct)
        calculate_rankings(board_type, season_id, game_day=total_game_days)

        # Find matching reward configs
        configs = RewardConfig.objects.filter(
            board_type=board_type,
            settlement_type=SettlementType.DAILY,
        )

        entries = LeaderboardEntry.objects.filter(
            board_type=board_type,
            season_id=season_id,
            game_day=total_game_days,
        )

        if player_id is not None:
            entries = entries.filter(character_id=player_id)

        for entry in entries:
            # 负分不发奖
            if entry.score <= 0:
                continue

            reward_id = f"daily_{board_type}_{season_id}_day{total_game_days}_{entry.character_id}"

            # Skip if already exists (idempotent)
            if PendingReward.objects.filter(reward_id=reward_id).exists():
                continue

            # Collect all matching reward configs into one rewards list
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
                    settlement_type=SettlementType.DAILY,
                    rank=entry.rank,
                    rewards=rewards,
                    expire_time=expire_ts,
                )


def settle_milestone_for_player(player_id, total_game_days, season_id="default"):
    """Settle milestone (total-board) rewards for a single player at every N game days.

    Only creates PendingReward for the given player. Rankings are still calculated
    globally so rank positions are correct."""
    now_ts = int(time.time())
    expire_ts = now_ts + 86400 * 7

    total_board_types = [BoardType.TOTAL_WEALTH, BoardType.TOTAL_OUTPUT, BoardType.TOTAL_FRESHNESS]

    for board_type in total_board_types:
        # Recalculate global rankings for the total board
        calculate_rankings(board_type, season_id, game_day=total_game_days)

        configs = RewardConfig.objects.filter(
            board_type=board_type,
            settlement_type=SettlementType.MILESTONE,
        )

        entry = LeaderboardEntry.objects.filter(
            board_type=board_type,
            season_id=season_id,
            character_id=player_id,
        ).first()

        if not entry or entry.score <= 0:
            continue

        reward_id = f"milestone_{board_type}_{season_id}_day{total_game_days}_{player_id}"

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
                player_id=player_id,
                reward_id=reward_id,
                board_type=board_type,
                settlement_type=SettlementType.MILESTONE,
                rank=entry.rank,
                rewards=rewards,
                expire_time=expire_ts,
            )


def reset_player_action_points(player_id):
    """Reset a single player's action_points to 100."""
    try:
        player = PlayerState.objects.get(player_id=player_id)
        player.action_points = 100
        player.save(update_fields=["action_points"])
    except PlayerState.DoesNotExist:
        pass


def daily_settlement(total_game_days, season_id="default"):
    """Run daily settlement: calculate rankings, generate rewards, reset players' AP for current season."""
    settle_leaderboard_for_day(total_game_days, season_id)
    # Only reset AP for players in the current season
    PlayerState.objects.filter(current_season_id=season_id).update(action_points=100)


def weekly_settlement(season_id, total_game_days):
    """Run weekly settlement: grant titles and buffs."""
    now_ts = int(time.time())
    expire_ts = now_ts + 86400 * 14  # Rewards expire in 14 days

    for board_type in BoardType.values:
        configs = RewardConfig.objects.filter(
            board_type=board_type,
            settlement_type=SettlementType.WEEKLY,
        )

        entries = LeaderboardEntry.objects.filter(
            board_type=board_type,
            season_id=season_id,
        )

        for entry in entries:
            if entry.score <= 0:
                continue

            reward_id = f"weekly_{board_type}_{season_id}_{entry.character_id}"

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
                    settlement_type=SettlementType.WEEKLY,
                    rank=entry.rank,
                    rewards=rewards,
                    expire_time=expire_ts,
                )


def claim_reward(reward_id, player_id):
    """Claim a pending reward. Returns (success, message, applied_effects)."""
    try:
        reward = PendingReward.objects.get(reward_id=reward_id, player_id=player_id)
    except PendingReward.DoesNotExist:
        return False, "reward not found", {}

    if reward.claimed:
        return False, "already claimed", {}

    # Apply reward effects
    effects = {}
    for r in reward.rewards:
        reward_type = r.get("type", "")

        if reward_type == "actionPoint":
            amount = r.get("amount", 0)
            try:
                player = PlayerState.objects.get(player_id=player_id)
                player.action_points = min(player.action_points + amount, player.action_points_max)
                player.save()
                effects["action_points"] = player.action_points
            except PlayerState.DoesNotExist:
                pass

        elif reward_type == "title":
            title_id = r.get("titleId", "")
            title_name = r.get("titleName", "")
            try:
                player = PlayerState.objects.get(player_id=player_id)
                PlayerTitle.objects.get_or_create(
                    player=player,
                    title_id=title_id,
                    defaults={
                        "title_name": title_name,
                        "obtained_at": int(time.time()),
                        "is_active": False,
                    },
                )
                effects["title_granted"] = title_name
            except PlayerState.DoesNotExist:
                pass

        elif reward_type == "item":
            item_id = r.get("itemId", 0)
            amount = r.get("amount", 1)
            if item_id:
                try:
                    account = GameAccount.objects.get(id=player_id)
                    bag, _ = InventoryBag.objects.get_or_create(
                        player_id=player_id,
                        character_id=account.username,
                        defaults={"money": 0},
                    )
                    from inventory.views import find_empty_slot
                    from items.models import Item as ItemModel
                    existing = bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
                    if existing:
                        existing.item_amount += amount
                        existing.save()
                    else:
                        slot = find_empty_slot(bag)
                        slot.item_id = item_id
                        slot.item_amount = amount
                        tmpl = ItemModel.objects.filter(item_id=item_id).first()
                        if tmpl and tmpl.max_use_count:
                            slot.remaining_uses = tmpl.max_use_count
                        item_name = tmpl.item_name if tmpl else f"物品#{item_id}"
                        slot.item_bag_name = f"{item_name}_{slot.slot_index}"
                        slot.save()
                    effects.setdefault("items_granted", []).append({"itemId": item_id, "amount": amount})
                except GameAccount.DoesNotExist:
                    pass

        elif reward_type == "randomRecipe":
            # 奖励一个菜谱盒子（itemId 3031），使用后随机获得菜谱
            item_id = 3031
            try:
                account = GameAccount.objects.get(id=player_id)
                bag, _ = InventoryBag.objects.get_or_create(
                    player_id=player_id,
                    character_id=account.username,
                    defaults={"money": 0},
                )
                from inventory.views import find_empty_slot
                from items.models import Item as ItemModel
                existing = bag.slots.filter(item_id=item_id, item_amount__gt=0).first()
                if existing:
                    existing.item_amount += 1
                    existing.save()
                else:
                    slot = find_empty_slot(bag)
                    slot.item_id = item_id
                    slot.item_amount = 1
                    tmpl = ItemModel.objects.filter(item_id=item_id).first()
                    if tmpl and tmpl.max_use_count:
                        slot.remaining_uses = tmpl.max_use_count
                    item_name = tmpl.item_name if tmpl else f"物品#{item_id}"
                    slot.item_bag_name = f"{item_name}_{slot.slot_index}"
                    slot.save()
                effects.setdefault("items_granted", []).append({"itemId": item_id, "amount": 1})
            except GameAccount.DoesNotExist:
                pass

        elif reward_type == "money":
            amount = r.get("amount", 0)
            if amount:
                try:
                    account = GameAccount.objects.get(id=player_id)
                    bag, _ = InventoryBag.objects.get_or_create(
                        player_id=player_id,
                        character_id=account.username,
                        defaults={"money": 0},
                    )
                    bag.money += amount
                    bag._money_change_source = "leaderboard_reward"
                    bag.save()
                    effects["money_granted"] = amount
                except GameAccount.DoesNotExist:
                    pass

        elif reward_type == "buff":
            # Buffs are tracked client-side based on reward data
            pass

    reward.claimed = True
    reward.save()

    return True, "OK", effects
