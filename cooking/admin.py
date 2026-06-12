from django.contrib import admin
from .models import PityConfig, PityCounter


@admin.register(PityConfig)
class PityConfigAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "quality_4_normal_phase",
        "quality_4_guarantee_at",
        "quality_5_normal_phase",
        "quality_5_guarantee_at",
        "quality_6_normal_phase",
        "quality_6_guarantee_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        # 只允许一条配置
        return not PityConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PityCounter)
class PityCounterAdmin(admin.ModelAdmin):
    list_display = ("player", "count_4", "count_5", "count_6", "updated_at")
    search_fields = ("player__username",)
    list_filter = ("updated_at",)
    readonly_fields = ("player",)
