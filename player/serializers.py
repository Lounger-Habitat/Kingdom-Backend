from rest_framework import serializers
from .models import PlayerState, PlayerRecipe, PlayerTitle


class PlayerStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlayerState
        fields = [
            "action_points",
            "action_points_max",
            "action_points_recovered_at",
            "current_season_id",
        ]


class PlayerTitleSerializer(serializers.ModelSerializer):
    titleId = serializers.CharField(source="title_id")
    titleName = serializers.CharField(source="title_name")
    obtainedAt = serializers.IntegerField(source="obtained_at")

    class Meta:
        model = PlayerTitle
        fields = ["titleId", "titleName", "obtainedAt"]
