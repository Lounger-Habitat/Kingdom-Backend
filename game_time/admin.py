from django.contrib import admin

from .models import GlobalGameTimeState


@admin.register(GlobalGameTimeState)
class GlobalGameTimeStateAdmin(admin.ModelAdmin):
    """服务端权威时间仅供后台查看，不允许人工修改。"""

    list_display = ["world_time", "current_season_id", "season", "updated_at"]
    readonly_fields = [
        "game_year", "game_month", "game_day", "game_hour", "game_minute",
        "season", "current_season_id", "updated_at",
    ]
    fieldsets = (
        ("当前游戏世界时间", {"fields": ("game_year", "game_month", "game_day", "game_hour", "game_minute")}),
        ("赛季信息", {"fields": ("current_season_id", "season")}),
        ("同步信息", {"fields": ("updated_at",)}),
    )

    @admin.display(description="当前游戏世界时间", ordering=("game_year", "game_month", "game_day", "game_hour", "game_minute"))
    def world_time(self, obj):
        return f"{obj.game_year:04d}-{obj.game_month:02d}-{obj.game_day:02d} {obj.game_hour:02d}:{obj.game_minute:02d}"

    def changelist_view(self, request, extra_context=None):
        GlobalGameTimeState.get_instance()
        return super().changelist_view(request, extra_context=extra_context)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
