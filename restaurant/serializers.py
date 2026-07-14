from rest_framework import serializers
from .models import (
    RestaurantConfig, RestaurantLevelConfig, PlayerRestaurantState,
    MenuItem, CustomerTransaction, DailyBusinessLog
)


class RestaurantConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = RestaurantConfig
        fields = [
            "business_hour", "business_minute",
            "base_customer_count", "customers_per_level",
            "llm_review_endpoint", "customer_name_pool"
        ]


class RestaurantLevelConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = RestaurantLevelConfig
        fields = "__all__"


class PlayerRestaurantStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlayerRestaurantState
        fields = [
            "current_level", "total_dishes_sold", "total_gold_earned",
            "total_ratings_received", "dishes_sold_set", "last_business_game_day",
            "season_id"
        ]


class MenuItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuItem
        fields = [
            "id", "instance_id", "item_id", "item_name", "dish_quality",
            "rated", "rating_price", "overall_score", "ingredient",
            "cook_time", "quantity", "exhibited_game_day"
        ]


class CustomerTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerTransaction
        fields = [
            "id", "season_id", "game_day", "customer_name", "item_id",
            "item_name", "dish_quality", "amount_paid", "rating",
            "review_comment", "created_at"
        ]


class DailyBusinessLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyBusinessLog
        fields = ["id", "game_day", "season_id", "customer_count", "total_earned", "created_at"]


class ExhibitDishSerializer(serializers.Serializer):
    """上架菜品请求序列化器。"""
    instance_id = serializers.CharField(help_text="背包物品实例ID")
    item_id = serializers.IntegerField(help_text="菜品ID")
    item_name = serializers.CharField(max_length=100, help_text="菜品名称")
    dish_quality = serializers.IntegerField(min_value=1, max_value=6, default=3)
    rated = serializers.BooleanField(default=False)
    rating_price = serializers.IntegerField(default=-1)
    overall_score = serializers.FloatField(default=0)
    ingredient = serializers.CharField(max_length=200, default="", allow_blank=True)
    cook_time = serializers.IntegerField(default=0)
    quantity = serializers.IntegerField(min_value=1, default=1)


class WithdrawDishSerializer(serializers.Serializer):
    """下架菜品请求序列化器。"""
    menu_item_id = serializers.IntegerField(help_text="菜单菜品ID")


class UpgradeRestaurantSerializer(serializers.Serializer):
    """升级餐厅请求序列化器。"""
    pass  # 无需参数，根据当前状态自动升级


class BusinessResultSerializer(serializers.Serializer):
    """营业结果序列化器。"""
    customer_name = serializers.CharField()
    item_id = serializers.IntegerField()
    item_name = serializers.CharField()
    dish_quality = serializers.IntegerField()
    amount_paid = serializers.IntegerField()
    rating = serializers.IntegerField()
    review_comment = serializers.CharField()
    destroyed_dishes = serializers.IntegerField(default=0, help_text="被销毁的剩余菜品数")
