from rest_framework import serializers
from .models import PlayerState, PlayerRecipe, PlayerTitle


class PlayerStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlayerState
        fields = [
            "action_points",
            "action_points_max",
            "game_day",
            "game_year",
            "game_month",
            "game_hour",
            "game_minute",
            "season",
            "total_game_days",
            "time_initialized",
        ]


class PlayerTitleSerializer(serializers.ModelSerializer):
    titleId = serializers.CharField(source="title_id")
    titleName = serializers.CharField(source="title_name")
    obtainedAt = serializers.IntegerField(source="obtained_at")

    class Meta:
        model = PlayerTitle
        fields = ["titleId", "titleName", "obtainedAt"]
