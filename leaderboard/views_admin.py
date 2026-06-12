import time

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.shortcuts import render, redirect
from django.urls import reverse

from .models import Season, LeaderboardEntry, ReportEvent, PendingReward
from .season_service import full_reset_to_season_1, rotate_season, settle_and_transition_old_season
from player.models import PlayerState
from cooking.models import PityCounter


@staff_member_required
def season_reset_confirm(request):
    """全量重置回第一赛季 — 确认页面。"""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "full_reset":
            try:
                stats = full_reset_to_season_1()
                parts = [
                    f"删除赛季: {stats.get('seasons_deleted', 0)}",
                    f"排行榜条目: {stats.get('leaderboard_entries_deleted', 0)}",
                    f"上报事件: {stats.get('report_events_deleted', 0)}",
                    f"待领奖励: {stats.get('pending_rewards_deleted', 0)}",
                    f"玩家状态重置: {stats.get('players_reset', 0)}",
                    f"背包重置: {stats.get('inventory_bags_reset', 0)}",
                    f"食谱重建: {stats.get('recipes_created', 0)}",
                    f"称号清除: {stats.get('titles_deleted', 0)}",
                    f"保底计数器重置: {stats.get('pity_counters_reset', 0)}",
                ]
                messages.success(request, "全量重置完成！" + "；".join(parts))
            except Exception as e:
                messages.error(request, f"重置失败: {e}")
            return redirect("leaderboard-season-reset-confirm")

    # 当前数据概览
    context = {
        "title": "全量重置回第一赛季",
        "current_season_count": Season.objects.count(),
        "active_season": Season.objects.filter(status="active").order_by("-sequence").first(),
        "leaderboard_entry_count": LeaderboardEntry.objects.count(),
        "report_event_count": ReportEvent.objects.count(),
        "pending_reward_count": PendingReward.objects.count(),
        "player_count": PlayerState.objects.count(),
        "pity_counter_count": PityCounter.objects.count(),
    }
    return render(request, "admin/leaderboard/season_reset/confirm.html", context)


@staff_member_required
def season_test_panel(request):
    """赛季轮转测试面板 — 手动触发各阶段操作。"""
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "force_expire":
            season = Season.objects.filter(status="active").order_by("-sequence").first()
            if season:
                season.end_time = int(time.time()) - 10
                season.save(update_fields=["end_time"])
                messages.success(request, f"已将 {season.season_id} 的 end_time 设为过去，现在可以触发轮转。")
            else:
                messages.error(request, "没有活跃的赛季。")

        elif action == "rotate":
            try:
                new_season = rotate_season()
                old = Season.objects.filter(status="ended").order_by("-sequence").first()
                if old:
                    messages.success(request, f"轮转完成：旧赛季 {old.season_id} → ended，新赛季 {new_season.season_id}。")
                else:
                    messages.info(request, f"当前赛季 {new_season.season_id} 未到期，无需轮转。")
            except Exception as e:
                messages.error(request, f"轮转失败: {e}")

        elif action == "settle":
            try:
                ended = Season.objects.filter(status="ended", settled=False)
                if not ended.exists():
                    messages.warning(request, "没有已结束但未结算的赛季，请先触发轮转。")
                else:
                    settle_and_transition_old_season()
                    settled_ids = list(ended.values_list("season_id", flat=True))
                    messages.success(request, f"结算完成：{', '.join(settled_ids)}。遗留玩家已强制迁移。")
            except Exception as e:
                messages.error(request, f"结算失败: {e}")

        return redirect("leaderboard-season-test")

    # 赛季列表（附带玩家数）
    seasons = Season.objects.all().order_by("-sequence")
    player_counts = dict(
        PlayerState.objects.values_list("current_season_id")
        .annotate(cnt=Count("id"))
        .values_list("current_season_id", "cnt")
    )
    season_rows = []
    for s in seasons:
        season_rows.append({
            "season_id": s.season_id,
            "sequence": s.sequence,
            "status": s.status,
            "settled": s.settled,
            "start_time": s.start_time,
            "end_time": s.end_time,
            "player_count": player_counts.get(s.season_id, 0),
        })

    # 玩家赛季分布
    distribution = list(
        PlayerState.objects.values("current_season_id")
        .annotate(cnt=Count("id"))
        .order_by("current_season_id")
    )

    active_season = Season.objects.filter(status="active").order_by("-sequence").first()
    ended_seasons = Season.objects.filter(status="ended", settled=False)

    context = {
        "title": "赛季轮转测试",
        "season_rows": season_rows,
        "active_season": active_season,
        "ended_seasons": ended_seasons,
        "distribution": distribution,
        "player_count": PlayerState.objects.count(),
    }
    return render(request, "admin/leaderboard/season_test/panel.html", context)
