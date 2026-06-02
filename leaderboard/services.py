"""Business logic for leaderboard ranking, daily/weekly settlement."""
import json
import time
from django.db.models import Sum, Max, Count
from .models import (
    LeaderboardEntry, RewardConfig, PendingReward, ReportEvent, BoardType, SettlementType,
)
from player.models import PlayerState, PlayerTitle
from account.models import GameAccount


def calculate_rankings_realtime(board_type, player_id=None, game_day=None):
    """Real-time ranking calculation from ReportEvent, without writing to DB.

    Returns (entries_list, my_rank_dict):
      - entries_list: list of dicts with rank, characterId, displayName, isAI, score, title, avatarId
      - my_rank_dict: dict with rank, score for the given player_id, or None
    """
    if board_type == BoardType.WEALTH:
        qs = ReportEvent.objects.filter(event_type="trade_completed")
        if game_day is not None:
            qs = qs.filter(game_day=game_day)
        events = qs.values("player_id", "payload")
        scores = {}
        for ev in events:
            try:
                payload = ev.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if payload.get("trade_type") == "sell":
                    pid = ev["player_id"]
                    scores[pid] = scores.get(pid, 0) + payload.get("total_price", 0)
            except (json.JSONDecodeError, TypeError):
                continue

    elif board_type == BoardType.OUTPUT:
        qs = ReportEvent.objects.filter(event_type="cook_completed")
        if game_day is not None:
            qs = qs.filter(game_day=game_day)
        counts = qs.values("player_id").annotate(count=Count("id"))
        scores = {c["player_id"]: c["count"] for c in counts}

    elif board_type == BoardType.FRESHNESS:
        qs = ReportEvent.objects.filter(event_type="judge_confirmed")
        if game_day is not None:
            qs = qs.filter(game_day=game_day)
        events = qs.values("player_id", "payload")
        scores = {}
        dish_names = {}
        for ev in events:
            try:
                payload = ev.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload)
                pid = ev["player_id"]
                score = payload.get("judge_score", 0)
                if score > scores.get(pid, 0):
                    scores[pid] = score
                    dish_names[pid] = payload.get("dish_name", "")
            except (json.JSONDecodeError, TypeError):
                continue
    else:
        return [], None

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # Batch fetch player names and titles to avoid N+1
    player_ids = [pid for pid, _ in ranked]
    usernames = {}
    for acc in GameAccount.objects.filter(id__in=player_ids):
        usernames[str(acc.id)] = acc.username

    active_titles = {}
    for pt in PlayerTitle.objects.filter(player_id__in=player_ids, is_active=True):
        active_titles[pt.player_id] = pt.title_name

    entries = []
    my_rank = None
    for rank, (pid, score) in enumerate(ranked, start=1):
        entry = {
            "rank": rank,
            "characterId": pid,
            "displayName": usernames.get(pid, pid),
            "isAI": False,
            "score": score,
            "title": active_titles.get(pid, ""),
            "avatarId": f"avatar_{pid}",
            "dishName": dish_names.get(pid, "") if board_type == BoardType.FRESHNESS else "",
        }
        entries.append(entry)
        if pid == player_id:
            my_rank = {"rank": rank, "score": score}

    return entries, my_rank


def calculate_rankings(board_type, season_id="default", game_day=None):
    """Calculate rankings for a given board type based on ReportEvents."""
    # Clear existing entries for this board type + day (all seasons) to avoid duplicates
    qs = LeaderboardEntry.objects.filter(board_type=board_type)
    if game_day is not None:
        qs = qs.filter(game_day=game_day)
    qs.delete()

    if board_type == BoardType.WEALTH:
        qs = ReportEvent.objects.filter(event_type="trade_completed")
        if game_day is not None:
            qs = qs.filter(game_day=game_day)
        events = qs.values("player_id", "payload")
        scores = {}
        for ev in events:
            try:
                payload = ev.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if payload.get("trade_type") == "sell":
                    pid = ev["player_id"]
                    scores[pid] = scores.get(pid, 0) + payload.get("total_price", 0)
            except (json.JSONDecodeError, TypeError):
                continue

    elif board_type == BoardType.OUTPUT:
        qs = ReportEvent.objects.filter(event_type="cook_completed")
        if game_day is not None:
            qs = qs.filter(game_day=game_day)
        counts = qs.values("player_id").annotate(count=Count("id"))
        scores = {c["player_id"]: c["count"] for c in counts}

    elif board_type == BoardType.FRESHNESS:
        qs = ReportEvent.objects.filter(event_type="judge_confirmed")
        if game_day is not None:
            qs = qs.filter(game_day=game_day)
        events = qs.values("player_id", "payload")
        scores = {}
        dish_names = {}
        for ev in events:
            try:
                payload = ev.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload)
                pid = ev["player_id"]
                score = payload.get("judge_score", 0)
                if score > scores.get(pid, 0):
                    scores[pid] = score
                    dish_names[pid] = payload.get("dish_name", "")
            except (json.JSONDecodeError, TypeError):
                continue
    else:
        return

    # Sort by score descending
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # Batch fetch usernames
    player_ids = [pid for pid, _ in ranked]
    usernames = {}
    for acc in GameAccount.objects.filter(id__in=player_ids):
        usernames[str(acc.id)] = acc.username

    entries = []
    for rank, (player_id, score) in enumerate(ranked, start=1):
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
            is_ai=False,
            score=score,
            rank=rank,
            title=title,
            avatar_id=f"avatar_{player_id}",
            dish_name=dish_names.get(player_id, "") if board_type == BoardType.FRESHNESS else "",
            game_day=game_day,
        ))

    LeaderboardEntry.objects.bulk_create(entries)


def settle_leaderboard_for_day(total_game_days, season_id="default"):
    """Recalculate rankings and generate rewards for a given cumulative game day.

    `total_game_days` is the player/global cumulative day count ("第 N 天")，
    保证全局唯一，避免月末 day-of-month 翻转导致 reward_id 冲突。
    Idempotent: PendingReward's unique reward_id prevents duplicates."""
    now_ts = int(time.time())
    expire_ts = now_ts + 86400 * 7  # Rewards expire in 7 days

    for board_type in [BoardType.WEALTH, BoardType.OUTPUT, BoardType.FRESHNESS]:
        # Calculate current rankings for this specific day
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

        for entry in entries:
            reward_id = f"daily_{board_type}_day{total_game_days}_{entry.character_id}"

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


def reset_player_action_points(player_id):
    """Reset a single player's action_points to 100."""
    try:
        player = PlayerState.objects.get(player_id=player_id)
        player.action_points = 100
        player.save(update_fields=["action_points"])
    except PlayerState.DoesNotExist:
        pass


def daily_settlement(total_game_days, season_id="default"):
    """Run daily settlement: calculate rankings, generate rewards, reset ALL players' AP."""
    settle_leaderboard_for_day(total_game_days, season_id)
    PlayerState.objects.all().update(action_points=100)


def weekly_settlement(season_id, total_game_days):
    """Run weekly settlement: grant titles and buffs."""
    now_ts = int(time.time())
    expire_ts = now_ts + 86400 * 14  # Rewards expire in 14 days

    for board_type in [BoardType.WEALTH, BoardType.OUTPUT, BoardType.FRESHNESS]:
        configs = RewardConfig.objects.filter(
            board_type=board_type,
            settlement_type=SettlementType.WEEKLY,
        )

        entries = LeaderboardEntry.objects.filter(
            board_type=board_type,
            season_id=season_id,
        )

        for entry in entries:
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
            # Item rewards are handled client-side via the reward data
            pass

        elif reward_type == "buff":
            # Buffs are tracked client-side based on reward data
            pass

    reward.claimed = True
    reward.save()

    return True, "OK", effects
