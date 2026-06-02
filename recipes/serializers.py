from rest_framework import serializers
from .models import Recipe, RecipeIngredient


class RecipeIngredientSerializer(serializers.ModelSerializer):
    itemID = serializers.IntegerField(source="item_id")
    itemName = serializers.CharField(source="item_name")

    class Meta:
        model = RecipeIngredient
        fields = ["itemID", "count", "itemName"]


class RecipeSerializer(serializers.ModelSerializer):
    itemID = serializers.IntegerField(source="item_id")
    recipeName = serializers.CharField(source="recipe_name")
    dishCategory = serializers.CharField(source="dish_category")
    servingStyle = serializers.CharField(source="serving_style")
    tasteProfile = serializers.CharField(source="taste_profile")
    defaultCookTime = serializers.IntegerField(source="default_cook_time")
    defaultDishQuality = serializers.IntegerField(source="default_dish_quality")
    ingredients = RecipeIngredientSerializer(many=True, read_only=True)

    class Meta:
        model = Recipe
        fields = [
            "itemID", "recipeName",
            "cuisine", "dishCategory", "servingStyle", "tasteProfile",
            "difficulty", "defaultCookTime", "defaultDishQuality",
            "ingredients",
        ]
