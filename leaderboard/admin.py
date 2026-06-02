from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import path
from django.utils.html import format_html
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from .models import RewardConfig, LeaderboardEntry, ReportEvent, PendingReward


class RewardConfigResource(resources.ModelResource):
    class Meta:
        model = RewardConfig
        import_id_fields = ["id"]


@admin.register(RewardConfig)
class RewardConfigAdmin(ImportExportModelAdmin):
    resource_class = RewardConfigResource
    list_display = ["board_type", "settlement_type", "rank_min", "rank_max", "reward_type", "reward_data"]
    list_filter = ["board_type", "settlement_type", "reward_type"]
    search_fields = ["board_type"]


@admin.register(LeaderboardEntry)
class LeaderboardEntryAdmin(admin.ModelAdmin):
    list_display = ["board_type", "game_day", "rank", "character_id", "display_name", "score", "is_ai"]
    list_filter = ["board_type", "game_day", "is_ai"]
    search_fields = ["character_id", "display_name"]
    readonly_fields = ["board_type", "season_id", "character_id", "display_name", "is_ai", "score", "rank", "title", "avatar_id", "game_day"]

    def get_urls(self):
        custom_urls = [
            path("history/", self.admin_site.admin_view(self.history_view), name="leaderboard-entry-history"),
        ]
        return custom_urls + super().get_urls()

    def history_view(self, request):
        return HttpResponseRedirect("/leaderboard/history/")

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["history_link"] = "/admin/leaderboard/leaderboardentry/history/"
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(ReportEvent)
class ReportEventAdmin(admin.ModelAdmin):
    list_display = ["player_id", "event_type", "game_day", "timestamp"]
    list_filter = ["event_type", "game_day"]
    search_fields = ["player_id"]
    readonly_fields = ["player_id", "event_type", "payload", "timestamp", "game_day"]

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(event_type="heartbeat")


@admin.register(PendingReward)
class PendingRewardAdmin(admin.ModelAdmin):
    list_display = ["player_id", "reward_id", "board_type", "settlement_type", "rank", "claimed", "expire_time"]
    list_filter = ["board_type", "settlement_type", "claimed"]
    search_fields = ["player_id", "reward_id"]
    readonly_fields = ["player_id", "reward_id", "board_type", "settlement_type", "rank", "rewards", "expire_time"]
