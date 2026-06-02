from rest_framework import serializers
from .models import LeaderboardEntry, PendingReward


class LeaderboardEntrySerializer(serializers.ModelSerializer):
    characterId = serializers.CharField(source="character_id")
    displayName = serializers.CharField(source="display_name")
    isAI = serializers.BooleanField(source="is_ai")
    avatarId = serializers.CharField(source="avatar_id")
    dishName = serializers.CharField(source="dish_name", default="", required=False)

    class Meta:
        model = LeaderboardEntry
        fields = ["rank", "characterId", "displayName", "isAI", "score", "title", "avatarId", "dishName"]


class RewardItemSerializer(serializers.Serializer):
    type = serializers.CharField()
    amount = serializers.IntegerField(required=False, default=0)
    titleId = serializers.CharField(required=False, default="")
    titleName = serializers.CharField(required=False, default="")
    buffId = serializers.CharField(required=False, default="")
    description = serializers.CharField(required=False, default="")
    itemId = serializers.IntegerField(required=False, default=0)


class PendingRewardSerializer(serializers.ModelSerializer):
    rewardId = serializers.CharField(source="reward_id")
    boardType = serializers.CharField(source="board_type")
    settlementType = serializers.CharField(source="settlement_type")
    expireTime = serializers.IntegerField(source="expire_time")

    class Meta:
        model = PendingReward
        fields = ["rewardId", "boardType", "settlementType", "rank", "rewards", "expireTime"]
