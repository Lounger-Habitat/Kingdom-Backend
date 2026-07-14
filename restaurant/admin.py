from django.contrib import admin
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from .models import (
    RestaurantConfig, RestaurantLevelConfig, PlayerRestaurantState,
    MenuItem, CustomerTransaction, DailyBusinessLog
)


# ──────────────────────────────────────────────────────────────
#  资源定义（用于 import_export）
# ──────────────────────────────────────────────────────────────

class RestaurantConfigResource(resources.ModelResource):
    class Meta:
        model = RestaurantConfig


class RestaurantLevelConfigResource(resources.ModelResource):
    class Meta:
        model = RestaurantLevelConfig


class PlayerRestaurantStateResource(resources.ModelResource):
    class Meta:
        model = PlayerRestaurantState


class MenuItemResource(resources.ModelResource):
    class Meta:
        model = MenuItem


class CustomerTransactionResource(resources.ModelResource):
    class Meta:
        model = CustomerTransaction


class DailyBusinessLogResource(resources.ModelResource):
    class Meta:
        model = DailyBusinessLog


# ──────────────────────────────────────────────────────────────
#  Admin 注册
# ──────────────────────────────────────────────────────────────

@admin.register(RestaurantConfig)
class RestaurantConfigAdmin(ImportExportModelAdmin):
    resource_class = RestaurantConfigResource
    list_display = ["id", "business_hour", "business_minute",
                    "base_customer_count", "customers_per_level"]
    fieldsets = (
        ("营业时间", {
            "fields": ("business_hour", "business_minute")
        }),
        ("顾客数量", {
            "fields": ("base_customer_count", "customers_per_level")
        }),
        ("AI 配置", {
            "fields": ("llm_review_endpoint", "customer_name_pool")
        }),
    )

    def has_add_permission(self, request):
        # 单例模式，只允许一条记录
        return not RestaurantConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RestaurantLevelConfig)
class RestaurantLevelConfigAdmin(ImportExportModelAdmin):
    resource_class = RestaurantLevelConfigResource
    list_display = ["level", "description", "required_total_ratings",
                    "required_dishes_sold", "cost_money", "reward_customer_bonus"]
    list_display_links = ["level", "description"]
    fieldsets = (
        ("基本信息", {
            "fields": ("level", "description")
        }),
        ("升级条件", {
            "fields": ("required_total_ratings", "required_dishes_sold",
                       "required_dish_ids", "required_dish_scores",
                       "cost_money", "cost_items")
        }),
        ("升级奖励", {
            "fields": ("reward_customer_bonus", "reward_materials",
                       "reward_recipes", "reward_speed_bonus_pct",
                       "reward_price_bonus_pct")
        }),
    )


@admin.register(PlayerRestaurantState)
class PlayerRestaurantStateAdmin(ImportExportModelAdmin):
    resource_class = PlayerRestaurantStateResource
    list_display = ["player", "current_level", "total_dishes_sold",
                    "total_gold_earned", "total_ratings_received", "last_business_game_day"]
    list_display_links = ["player", "current_level"]
    list_filter = ["current_level", "season_id"]
    search_fields = ["player__username"]
    readonly_fields = ["dishes_sold_set"]
    actions = ["reset_daily_business"]

    @admin.action(description="清除今日营业记录（可重新营业）")
    def reset_daily_business(self, request, queryset):
        from player.models import PlayerState
        count = 0
        for state in queryset:
            player = state.player
            try:
                ps = PlayerState.objects.get(player_id=str(player.id))
                season_id = ps.current_season_id or ""
                DailyBusinessLog.objects.filter(
                    player=player,
                    season_id=season_id,
                    game_year=ps.game_year,
                    game_month=ps.game_month,
                    game_day_of_month=ps.game_day,
                ).delete()
                CustomerTransaction.objects.filter(
                    player=player, game_day=ps.total_game_days
                ).delete()
                if state.last_business_game_day == ps.total_game_days:
                    state.last_business_game_day = 0
                    state.save()
            except PlayerState.DoesNotExist:
                pass
            count += 1
        self.message_user(request, f"已清除 {count} 位玩家的今日营业记录")


@admin.register(MenuItem)
class MenuItemAdmin(ImportExportModelAdmin):
    resource_class = MenuItemResource
    list_display = ["player", "item_name", "dish_quality", "quantity",
                    "rated", "rating_price", "overall_score", "exhibited_game_day"]
    list_display_links = ["player", "item_name"]
    list_filter = ["dish_quality", "rated", "exhibited_game_day"]
    search_fields = ["player__username", "item_name"]


@admin.register(CustomerTransaction)
class CustomerTransactionAdmin(ImportExportModelAdmin):
    resource_class = CustomerTransactionResource
    list_display = ["player", "customer_name", "item_name", "dish_quality",
                    "amount_paid", "rating", "game_day", "created_at"]
    list_display_links = ["player", "customer_name"]
    list_filter = ["rating", "dish_quality", "game_day", "season_id"]
    search_fields = ["player__username", "customer_name", "item_name"]
    readonly_fields = ["created_at"]


class CustomerTransactionInline(admin.TabularInline):
    model = CustomerTransaction
    fk_name = "daily_log"
    extra = 0
    fields = ["customer_name", "item_name", "dish_quality", "amount_paid", "rating", "review_comment"]
    readonly_fields = fields
    can_delete = False


@admin.register(DailyBusinessLog)
class DailyBusinessLogAdmin(ImportExportModelAdmin):
    resource_class = DailyBusinessLogResource
    list_display = ["player", "season_id", "game_year", "game_month", "game_day_of_month", "customer_count", "total_earned", "created_at"]
    list_display_links = ["player"]
    list_filter = ["season_id", "game_year", "game_month"]
    search_fields = ["player__username"]
    readonly_fields = ["created_at"]
    inlines = [CustomerTransactionInline]
