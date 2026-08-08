from django.contrib import admin
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from .models import (
    RestaurantConfig, RestaurantLevelConfig, PlayerRestaurantState,
    MenuItem, CustomerTransaction, DailyBusinessLog, RestaurantHourlySlot, RestaurantSaleEvent
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


class ReadOnlyRecordAdmin(admin.ModelAdmin):
    """营业过程自动生成的记录仅供查询。"""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ──────────────────────────────────────────────────────────────
#  Admin 注册
# ──────────────────────────────────────────────────────────────

@admin.register(RestaurantConfig)
class RestaurantConfigAdmin(ImportExportModelAdmin):
    resource_class = RestaurantConfigResource
    list_display = ["id", "opening_hour", "closing_hour", "base_hourly_sales", "base_menu_capacity"]
    fieldsets = (
        ("营业时间", {
            "fields": ("opening_hour", "closing_hour")
        }),
        ("顾客数量", {
            "fields": ("base_hourly_sales", "hourly_sales_per_level", "base_menu_capacity", "menu_capacity_per_level")
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
            "fields": ("reward_customer_bonus", "reward_menu_capacity_bonus",
                       "reward_materials", "reward_recipes",
                       "reward_speed_bonus_pct", "reward_price_bonus_pct")
        }),
    )


@admin.register(PlayerRestaurantState)
class PlayerRestaurantStateAdmin(ImportExportModelAdmin):
    resource_class = PlayerRestaurantStateResource
    list_display = ["player", "current_level", "total_dishes_sold", "total_gold_earned", "total_ratings_received"]
    list_display_links = ["player", "current_level"]
    list_filter = ["current_level", "season_id"]
    search_fields = ["player__username"]
    readonly_fields = ["dishes_sold_set"]
    actions = []



@admin.register(MenuItem)
class MenuItemAdmin(ImportExportModelAdmin):
    resource_class = MenuItemResource
    list_display = ["player", "item_name", "dish_quality", "quantity", "rated", "rating_price", "overall_score"]
    list_display_links = ["player", "item_name"]
    list_filter = ["dish_quality", "rated"]
    search_fields = ["player__username", "item_name"]


@admin.register(CustomerTransaction)
class CustomerTransactionAdmin(ReadOnlyRecordAdmin, ImportExportModelAdmin):
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
class DailyBusinessLogAdmin(ReadOnlyRecordAdmin, ImportExportModelAdmin):
    resource_class = DailyBusinessLogResource
    list_display = ["player", "season_id", "game_year", "game_month", "game_day_of_month", "customer_count", "total_earned", "created_at"]
    list_display_links = ["player"]
    list_filter = ["season_id", "game_year", "game_month"]
    search_fields = ["player__username"]
    readonly_fields = ["created_at"]
    inlines = [CustomerTransactionInline]


@admin.register(RestaurantHourlySlot)
class RestaurantHourlySlotAdmin(ReadOnlyRecordAdmin):
    list_display = ["player", "game_year", "game_month", "game_day", "game_hour", "quota", "sold_count", "restaurant_level"]
    list_filter = ["game_year", "game_month", "game_day", "game_hour"]
    search_fields = ["player__username"]
    readonly_fields = ["player", "game_year", "game_month", "game_day", "game_hour", "quota", "sold_count", "restaurant_level"]


@admin.register(RestaurantSaleEvent)
class RestaurantSaleEventAdmin(ReadOnlyRecordAdmin):
    list_display = ["slot", "scheduled_minute", "processed", "skipped", "transaction", "created_at"]
    list_filter = ["processed", "skipped"]
    readonly_fields = ["slot", "scheduled_minute", "processed", "skipped", "transaction", "created_at"]
