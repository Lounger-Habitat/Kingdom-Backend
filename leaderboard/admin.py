import copy
from django.contrib import admin
from django.db.models import Count
from django.http import HttpResponseRedirect, QueryDict
from django.shortcuts import redirect
from django.urls import path
from django.utils.html import format_html
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from .models import RewardConfig, LeaderboardEntry, ReportEvent, PendingReward, LeaderboardPeriodConfig
from season.models import Season


class RewardConfigResource(resources.ModelResource):
    class Meta:
        model = RewardConfig
        import_id_fields = ["id"]


@admin.register(RewardConfig)
class RewardConfigAdmin(ImportExportModelAdmin):
    resource_class = RewardConfigResource
    list_display = ["period_config", "board_type", "settlement_type", "rank_min", "rank_max", "reward_type", "reward_data"]
    list_filter = ["board_type", "settlement_type", "reward_type"]
    search_fields = ["board_type"]


@admin.register(LeaderboardPeriodConfig)
class LeaderboardPeriodConfigAdmin(admin.ModelAdmin):
    list_display = ["name", "interval_hours", "enabled", "reward_expire_hours"]
    list_editable = ["enabled"]
    ordering = ["interval_hours", "name"]


class ReadOnlyRecordAdmin(admin.ModelAdmin):
    """系统生成的运行记录只允许查看，避免后台误操作破坏结算数据。"""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LeaderboardEntry)
class LeaderboardEntryAdmin(ReadOnlyRecordAdmin):
    list_display = ["season_id", "board_type", "game_day", "rank", "character_id", "display_name", "score", "is_ai"]
    list_filter = ["board_type", "is_ai"]
    search_fields = ["character_id", "display_name"]
    list_per_page = 50
    readonly_fields = ["board_type", "season_id", "character_id", "display_name", "is_ai", "score", "rank", "title", "avatar_id", "game_day"]

    # ───── 第三层：有 season_id + game_day 时走默认 changelist ─────

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        season_id = request.GET.get("season_id")
        game_day = request.GET.get("game_day")
        if season_id and game_day:
            qs = qs.filter(season_id=season_id)
            if game_day == "__season__":
                qs = qs.filter(game_day__isnull=True)
            else:
                qs = qs.filter(game_day=int(game_day))
        return qs

    def get_urls(self):
        custom_urls = [
            path("history/", self.admin_site.admin_view(self.history_view), name="leaderboard-entry-history"),
        ]
        return custom_urls + super().get_urls()

    def history_view(self, request):
        return HttpResponseRedirect("/leaderboard/history/")

    # ───── 三级路由：根据 URL 参数决定显示哪一层 ─────

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["history_link"] = "/admin/leaderboard/leaderboardentry/history/"

        season_id = request.GET.get("season_id")
        game_day = request.GET.get("game_day")

        # 第三层：赛季 + 天数都有，按榜单分组显示
        if season_id and game_day:
            qs = LeaderboardEntry.objects.filter(season_id=season_id)
            if game_day == "__season__":
                qs = qs.filter(game_day__isnull=True)
                day_label = "赛季结算快照"
            else:
                qs = qs.filter(game_day=int(game_day))
                day_label = f"第 {game_day} 天"

            from .models import BoardType
            board_groups = []
            for bt in BoardType.values:
                entries = qs.filter(board_type=bt).order_by("rank")
                if entries.exists():
                    board_groups.append({
                        "board_type": bt,
                        "board_label": dict(BoardType.choices).get(bt, bt),
                        "entries": entries,
                    })

            extra_context["season_id"] = season_id
            extra_context["game_day"] = game_day
            extra_context["day_label"] = day_label
            extra_context["board_groups"] = board_groups
            # 移除自定义参数，避免 ChangeList 处理时报错
            cleaned = copy.copy(request)
            cleaned.GET = QueryDict(request.GET.urlencode())
            for k in ("season_id", "game_day"):
                cleaned.GET = cleaned.GET.copy()
                cleaned.GET.pop(k, None)
            return super().changelist_view(cleaned, extra_context=extra_context)

        # 第二层：只有赛季，显示该赛季的游戏天数列表
        if season_id:
            day_rows = (
                LeaderboardEntry.objects
                .filter(season_id=season_id)
                .values("game_day")
                .annotate(entry_count=Count("id"))
                .order_by("-game_day")
            )
            day_list = []
            for row in day_rows:
                gd = row["game_day"]
                boards = (
                    LeaderboardEntry.objects
                    .filter(season_id=season_id, game_day=gd)
                    .values_list("board_type", flat=True)
                    .distinct()
                )
                day_list.append({
                    "game_day": gd,
                    "entry_count": row["entry_count"],
                    "board_types": "、".join(sorted(set(boards))),
                })
            extra_context["season_id"] = season_id
            extra_context["day_list"] = day_list
            return super().changelist_view(request, extra_context=extra_context)

        # 第一层：赛季列表
        season_rows = (
            LeaderboardEntry.objects
            .values("season_id")
            .annotate(entry_count=Count("id"), day_count=Count("game_day", distinct=True))
            .order_by("-season_id")
        )
        # 补充元数据
        season_map = {s.season_id: s for s in Season.objects.all()}
        season_list = []
        for row in season_rows:
            sid = row["season_id"]
            meta = season_map.get(sid)
            season_list.append({
                "season_id": sid,
                "sequence": meta.sequence if meta else None,
                "status": meta.status if meta else "unknown",
                "duration_days": meta.duration_days if meta else "?",
                "entry_count": row["entry_count"],
                "day_count": row["day_count"],
            })

        extra_context["season_list"] = season_list
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(ReportEvent)
class ReportEventAdmin(ReadOnlyRecordAdmin):
    list_display = ["player_id", "event_type", "game_day", "season_id", "timestamp"]
    list_filter = ["event_type", "game_day", "season_id"]
    search_fields = ["player_id"]
    readonly_fields = ["player_id", "event_type", "payload", "timestamp", "game_day", "season_id"]

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(event_type="heartbeat")


@admin.register(PendingReward)
class PendingRewardAdmin(ReadOnlyRecordAdmin):
    list_display = ["player_id", "reward_id", "board_type", "settlement_type", "rank", "claimed", "expire_time"]
    list_filter = ["board_type", "settlement_type", "claimed"]
    search_fields = ["player_id", "reward_id"]
    readonly_fields = ["player_id", "reward_id", "board_type", "settlement_type", "rank", "rewards", "expire_time"]
