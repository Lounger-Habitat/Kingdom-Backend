from rest_framework import serializers
from drf_spectacular.utils import extend_schema, OpenApiExample
from .models import InventoryBag, InventorySlot


class InventorySlotSerializer(serializers.ModelSerializer):
    instanceID = serializers.UUIDField(source="instance_id")
    itemID = serializers.IntegerField(source="item_id")
    itemAmount = serializers.IntegerField(source="item_amount")
    ratingPrice = serializers.IntegerField(source="rating_price")
    overallScore = serializers.IntegerField(source="overall_score")
    itemBagName = serializers.CharField(source="item_bag_name")
    cookTime = serializers.IntegerField(source="cook_time")
    dishQuality = serializers.IntegerField(source="dish_quality")
    hideInShop = serializers.BooleanField(source="hide_in_shop")

    class Meta:
        model = InventorySlot
        fields = ["instanceID", "itemID", "itemAmount", "rated", "ratingPrice", "overallScore", "ingredient", "itemBagName", "cookTime", "dishQuality", "hideInShop"]


def serialize_bag(bag):
    """Serialize an InventoryBag with all its slots (including empty ones)."""
    slots = bag.slots.all().order_by("slot_index")
    items = InventorySlotSerializer(slots, many=True).data
    return {"money": bag.money, "items": items}


def success_response(bag, message="操作成功"):
    return {"success": True, "message": message, "data": serialize_bag(bag)}


def dual_bag_response(from_bag, to_bag, message="操作成功"):
    return {
        "success": True,
        "message": message,
        "data": {
            "fromBag": serialize_bag(from_bag),
            "toBag": serialize_bag(to_bag),
        },
    }


def error_response(message):
    return {"success": False, "message": message}


class BagDataSerializer(serializers.Serializer):
    money = serializers.IntegerField()
    items = InventorySlotSerializer(many=True)


class InventoryResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    message = serializers.CharField()
    data = BagDataSerializer()


class DualBagDataSerializer(serializers.Serializer):
    fromBag = BagDataSerializer()
    toBag = BagDataSerializer()


class DualBagResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    message = serializers.CharField()
    data = DualBagDataSerializer()


class ErrorResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    message = serializers.CharField()
