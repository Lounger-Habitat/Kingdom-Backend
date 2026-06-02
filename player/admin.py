from django.contrib import admin
from django.db.models import Count
from django.shortcuts import render, redirect
from django.urls import path
from account.models import GameAccount
from .models import PlayerState, PlayerRecipe, PlayerTitle, DefaultRecipe, PlayerResetProxy


class PlayerRecipeInline(admin.TabularInline):
    model = PlayerRecipe
    extra = 0
    fields = ["recipe_name", "catalog_recipe"]
    ordering = ["recipe_name"]


class PlayerTitleInline(admin.TabularInline):
    model = PlayerTitle
    extra = 0
    fields = ["title_id", "title_name", "obtained_at", "is_active"]
    ordering = ["-is_active", "title_name"]


@admin.register(PlayerState)
class PlayerStateAdmin(admin.ModelAdmin):
    list_display = [
        "player_id",
        "recipe_count",
        "action_points",
        "action_points_max",
        "game_day",
        "game_year",
        "game_month",
        "season",
    ]
    list_filter = ["season", "game_year"]
    search_fields = ["player_id"]
    inlines = [PlayerRecipeInline, PlayerTitleInline]

    @admin.display(description="菜谱数")
    def recipe_count(self, obj):
        return obj.recipes.count()


@admin.register(PlayerRecipe)
class PlayerRecipeAdmin(admin.ModelAdmin):
    list_display = ["player", "recipe_name", "ingredients_display"]
    list_filter = ["player"]
    search_fields = ["player__player_id", "recipe_name"]

    @admin.display(description="食材")
    def ingredients_display(self, obj):
        if not obj.catalog_recipe:
            return ""
        return ", ".join(
            f"{ing.item_name}×{ing.count}"
            for ing in obj.catalog_recipe.ingredients.all()
        )

    def changelist_view(self, request, extra_context=None):
        # 未选玩家时，显示玩家列表
        if "player__id__exact" not in request.GET:
            players = (
                PlayerState.objects
                .filter(recipes__isnull=False)
                .annotate(recipe_count=Count("recipes"))
                .order_by("player_id")
                .distinct()
            )
            # 关联 GameAccount 获取用户名
            account_ids = [int(p.player_id) for p in players if p.player_id.isdigit()]
            accounts = {a.id: a.username for a in GameAccount.objects.filter(id__in=account_ids)}
            player_list = []
            for p in players:
                player_list.append({
                    "id": p.id,
                    "player_id": p.player_id,
                    "username": accounts.get(int(p.player_id), p.player_id) if p.player_id.isdigit() else p.player_id,
                    "recipe_count": p.recipe_count,
                })
            context = {**self.admin_site.each_context(request), "players": player_list, "title": "选择玩家查看菜谱"}
            return render(request, "admin/player/playerrecipe/player_list.html", context)

        # 已选玩家时，正常显示菜谱列表
        return super().changelist_view(request, extra_context)


@admin.register(PlayerTitle)
class PlayerTitleAdmin(admin.ModelAdmin):
    list_display = ["player", "title_id", "title_name", "is_active", "obtained_at"]
    search_fields = ["player__player_id", "title_id", "title_name"]
    list_filter = ["is_active"]

    def get_urls(self):
        custom_urls = [
            path("players/", self.admin_site.admin_view(self.player_list_view), name="playertitle-player-list"),
        ]
        return custom_urls + super().get_urls()

    change_list_template = "admin/player/playertitle/change_list.html"

    def changelist_view(self, request, extra_context=None):
        if "player__id__exact" not in request.GET:
            return self.player_list_view(request)
        extra_context = extra_context or {}
        player_id = request.GET.get("player__id__exact")
        if player_id:
            try:
                ps = PlayerState.objects.get(id=player_id)
                if ps.player_id.isdigit():
                    account = GameAccount.objects.filter(id=int(ps.player_id)).first()
                    extra_context["player_name"] = account.username if account else ps.player_id
                else:
                    extra_context["player_name"] = ps.player_id
            except PlayerState.DoesNotExist:
                pass
        return super().changelist_view(request, extra_context)

    def player_list_view(self, request):
        players = (
            PlayerState.objects
            .filter(titles__isnull=False)
            .annotate(title_count=Count("titles"))
            .order_by("player_id")
            .distinct()
        )
        account_ids = [int(p.player_id) for p in players if p.player_id.isdigit()]
        accounts = {a.id: a.username for a in GameAccount.objects.filter(id__in=account_ids)}
        player_list = []
        for p in players:
            active = p.titles.filter(is_active=True).first()
            player_list.append({
                "id": p.id,
                "player_id": p.player_id,
                "username": accounts.get(int(p.player_id), p.player_id) if p.player_id.isdigit() else p.player_id,
                "title_count": p.title_count,
                "active_title": active.title_name if active else "",
            })
        context = {**self.admin_site.each_context(request), "players": player_list, "title": "选择玩家查看称号"}
        return render(request, "admin/player/playertitle/player_list.html", context)


@admin.register(DefaultRecipe)
class DefaultRecipeAdmin(admin.ModelAdmin):
    list_display = ["recipe_name"]
    search_fields = ["recipe_name"]


@admin.register(PlayerResetProxy)
class PlayerResetAdmin(admin.ModelAdmin):
    """代理模型 Admin，点击后跳转到自定义的玩家重置列表页。"""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect("player-admin-reset-list")
