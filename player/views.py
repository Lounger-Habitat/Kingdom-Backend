from rest_framework.decorators import api_view
from rest_framework.response import Response
from config.auth import get_player_id
from recipes.models import Recipe as CatalogRecipe, RecipeIngredient
from game_time.models import GameTimeState
from .models import PlayerState, PlayerRecipe, PlayerTitle, DefaultRecipe


def get_or_create_player(player_id):
    player, _ = PlayerState.objects.get_or_create(player_id=player_id)
    return player


@api_view(["GET"])
def player_state(request):
    player_id = get_player_id(request)
    player = get_or_create_player(player_id)
    max_game_days = GameTimeState.get_instance().max_game_days
    return Response({
        "code": 0,
        "data": {
            "action_points": player.action_points,
            "action_points_max": player.action_points_max,
            "game_day": player.game_day,
            "game_year": player.game_year,
            "game_month": player.game_month,
            "game_hour": player.game_hour,
            "game_minute": player.game_minute,
            "season": player.season,
            "total_game_days": player.total_game_days,
            "time_initialized": player.time_initialized,
            "max_game_days": max_game_days,
        },
    })


@api_view(["POST"])
def player_cost_action(request):
    player_id = get_player_id(request)
    player = get_or_create_player(player_id)
    cost_type = request.data.get("cost_type", "")
    cost_amount = request.data.get("cost_amount", 0)

    if player.action_points < cost_amount:
        return Response({
            "code": "INSUFFICIENT_ACTION_POINTS",
            "message": "行动力不足",
        }, status=200)

    player.action_points -= cost_amount
    player.save()

    return Response({
        "code": 0,
        "data": {
            "action_points": player.action_points,
            "action_points_max": player.action_points_max,
            "game_day": player.game_day,
            "total_game_days": player.total_game_days,
        },
    })


def _serialize_catalog_ingredients(catalog_recipe):
    """从完整菜谱目录读取食材列表。"""
    return [
        {"itemName": ing.item_name, "count": ing.count}
        for ing in catalog_recipe.ingredients.all()
    ]


@api_view(["GET", "POST"])
def player_recipes(request):
    player_id = get_player_id(request)
    player = get_or_create_player(player_id)

    if request.method == "GET":
        player_recipes_qs = PlayerRecipe.objects.filter(player=player).select_related("catalog_recipe")

        # 新玩家无菜谱时，从默认菜谱列表复制（关联完整目录）
        if not player_recipes_qs.exists():
            defaults = DefaultRecipe.objects.all()
            if defaults.exists():
                to_create = []
                for d in defaults:
                    catalog = CatalogRecipe.objects.filter(recipe_name=d.recipe_name).first()
                    if catalog:
                        to_create.append(PlayerRecipe(
                            player=player,
                            recipe_name=d.recipe_name,
                            catalog_recipe=catalog,
                        ))
                if to_create:
                    PlayerRecipe.objects.bulk_create(to_create)
                player_recipes_qs = PlayerRecipe.objects.filter(player=player).select_related("catalog_recipe")

        # 拼装完整数据：食材从完整目录读取
        result = {}
        for pr in player_recipes_qs:
            ingredients = _serialize_catalog_ingredients(pr.catalog_recipe) if pr.catalog_recipe else []
            result[pr.recipe_name] = {
                "ingredients": ingredients,
                "showIngredientCount": pr.show_ingredient_count,
            }
        return Response(result)

    # POST：接受菜名列表或 { "菜名": { "showIngredientCount": bool } } 格式
    data = request.data
    skipped = []

    def _upsert_recipe(recipe_name, show_ingredient_count=None):
        catalog = CatalogRecipe.objects.filter(recipe_name=recipe_name).first()
        if catalog:
            defaults = {"catalog_recipe": catalog}
            if show_ingredient_count is not None:
                defaults["show_ingredient_count"] = show_ingredient_count
            PlayerRecipe.objects.update_or_create(
                player=player,
                recipe_name=recipe_name,
                defaults=defaults,
            )
        else:
            skipped.append(recipe_name)

    if "recipeNames" in data:
        for recipe_name in data["recipeNames"]:
            _upsert_recipe(recipe_name)
    elif isinstance(data, list):
        for recipe_name in data:
            if isinstance(recipe_name, str):
                _upsert_recipe(recipe_name)
    elif isinstance(data, dict):
        for recipe_name, value in data.items():
            show = value.get("showIngredientCount") if isinstance(value, dict) else None
            _upsert_recipe(recipe_name, show)

    resp = {"success": True, "message": "ok"}
    if skipped:
        resp["skipped"] = skipped
        resp["message"] = f"以下菜名在菜谱目录中不存在，已跳过: {', '.join(skipped)}"
    return Response(resp)


@api_view(["PATCH"])
def player_recipe_setting(request, recipe_name):
    """切换单道菜谱的 showIngredientCount 设置。"""
    player_id = get_player_id(request)
    player = get_or_create_player(player_id)

    try:
        pr = PlayerRecipe.objects.get(player=player, recipe_name=recipe_name)
    except PlayerRecipe.DoesNotExist:
        return Response({"success": False, "message": "菜谱不存在"}, status=404)

    show = request.data.get("showIngredientCount")
    if show is None:
        return Response({"success": False, "message": "缺少 showIngredientCount"}, status=400)

    pr.show_ingredient_count = bool(show)
    pr.save(update_fields=["show_ingredient_count"])

    return Response({"success": True, "showIngredientCount": pr.show_ingredient_count})


@api_view(["GET"])
def player_titles(request):
    player_id = get_player_id(request)
    player = get_or_create_player(player_id)
    titles = PlayerTitle.objects.filter(player=player)

    active_title = titles.filter(is_active=True).first()
    active_id = active_title.title_id if active_title else ""

    from .serializers import PlayerTitleSerializer
    serialized = PlayerTitleSerializer(titles, many=True).data

    return Response({
        "success": True,
        "data": {
            "titles": serialized,
            "activeTitleId": active_id,
        },
    })
