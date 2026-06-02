from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from .models import Recipe, RecipeIngredient
from .serializers import RecipeSerializer


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def recipe_list(request):
    if request.method == "POST":
        return _push_recipes(request)

    recipes = Recipe.objects.prefetch_related("ingredients").all()
    version = max((r.version for r in recipes), default=0)
    serialized = RecipeSerializer(recipes, many=True).data
    return Response({
        "version": version,
        "recipes": serialized,
    })


def _push_recipes(request):
    body = request.data
    recipes_data = body if isinstance(body, list) else body.get("recipes", [])

    if not isinstance(recipes_data, list):
        return Response({"error": "recipes 字段需要是数组"}, status=400)

    created, updated = 0, 0
    for item in recipes_data:
        item_id = item.get("itemID")
        recipe_name = item.get("recipeName", "")
        if item_id is None:
            continue

        recipe_obj, is_new = Recipe.objects.update_or_create(
            item_id=item_id,
            recipe_name=recipe_name,
            defaults={
                "recipe_name": recipe_name,
                "cuisine": item.get("cuisine", ""),
                "dish_category": item.get("dishCategory", ""),
                "serving_style": item.get("servingStyle", ""),
                "taste_profile": item.get("tasteProfile", ""),
                "difficulty": item.get("difficulty", 2),
                "default_cook_time": item.get("defaultCookTime", 0),
                "default_dish_quality": item.get("defaultDishQuality", 3),
            },
        )

        # 全量替换食材
        recipe_obj.ingredients.all().delete()
        for ing in item.get("ingredients", []):
            RecipeIngredient.objects.create(
                recipe=recipe_obj,
                item_id=ing.get("itemID", 0),
                item_name=ing.get("itemName", ""),
                count=ing.get("count", 1),
            )

        if is_new:
            created += 1
        else:
            updated += 1

    return Response({"created": created, "updated": updated})
