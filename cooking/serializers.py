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
