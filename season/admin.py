from django.contrib import admin
from .models import Season, SeasonConfig, SeasonResetProxy, SeasonTestProxy


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_display = ["season_id", "sequence", "status", "settled", "start_time_display", "end_time_display"]
    list_filter = ["status", "settled"]
    readonly_fields = ["season_id", "sequence", "start_time", "start_time_display", "end_time", "end_time_display", "settled"]

    @staticmethod
    def _fmt_epoch(ts):
        if not ts:
            return "-"
        import datetime
        from django.utils import timezone
        dt = datetime.datetime.fromtimestamp(int(ts), tz=timezone.get_current_timezone())
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    @admin.display(description="开始时间", ordering="start_time")
    def start_time_display(self, obj):
        return self._fmt_epoch(obj.start_time)

    @admin.display(description="结束时间", ordering="end_time")
    def end_time_display(self, obj):
        return self._fmt_epoch(obj.end_time)


@admin.register(SeasonConfig)
class SeasonConfigAdmin(admin.ModelAdmin):
    list_display = ["duration_days", "max_game_days", "auto_rotate", "freshness_top_n"]


@admin.register(SeasonResetProxy)
class SeasonResetAdmin(admin.ModelAdmin):
    """代理模型 Admin，点击后跳转到赛季重置确认页。"""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        return redirect("leaderboard-season-reset-confirm")


@admin.register(SeasonTestProxy)
class SeasonTestAdmin(admin.ModelAdmin):
    """代理模型 Admin，点击后跳转到赛季轮转测试面板。"""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        return redirect("leaderboard-season-test")
