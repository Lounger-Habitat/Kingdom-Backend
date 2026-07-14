from rest_framework import serializers


class PityCheckRequestSerializer(serializers.Serializer):
    recipeId = serializers.IntegerField()
    normalQuality = serializers.IntegerField(min_value=1, max_value=6)
    cookTime = serializers.IntegerField(min_value=0)
    defaultCookTime = serializers.IntegerField(min_value=0)
    ingredientsCorrect = serializers.BooleanField()


class PityCheckDataSerializer(serializers.Serializer):
    finalQuality = serializers.IntegerField()
    cookTime = serializers.IntegerField()
    pityCount4 = serializers.IntegerField()
    pityCount5 = serializers.IntegerField()
    pityCount6 = serializers.IntegerField()


class PityCheckResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    message = serializers.CharField(required=False, allow_blank=True)
    data = PityCheckDataSerializer(required=False)


# --- Cook endpoint serializers ---

class CookIngredientSerializer(serializers.Serializer):
    itemId = serializers.IntegerField()
    count = serializers.IntegerField(min_value=1)


class CookRequestSerializer(serializers.Serializer):
    characterId = serializers.CharField(max_length=100)
    ingredients = CookIngredientSerializer(many=True)


class CookResultSerializer(serializers.Serializer):
    recipeName = serializers.CharField()
    itemId = serializers.IntegerField()
    quality = serializers.IntegerField()
    cookTime = serializers.IntegerField()
    ingredientsExact = serializers.BooleanField()
    isNewlyLearned = serializers.BooleanField()
    ingredient = serializers.CharField()
    qualityName = serializers.CharField()
    pityCount4 = serializers.IntegerField()
    pityCount5 = serializers.IntegerField()
    pityCount6 = serializers.IntegerField()


class CookResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    message = serializers.CharField(required=False, allow_blank=True)
    cookResult = CookResultSerializer(required=False)
    data = serializers.DictField(required=False)
