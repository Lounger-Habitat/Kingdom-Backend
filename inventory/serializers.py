from rest_framework import serializers
from drf_spectacular.utils import extend_schema, OpenApiExample
from .models import InventoryBag, InventorySlot
from items.models import Item


class InventorySlotSerializer(serializers.ModelSerializer):
    slotIndex = serializers.IntegerField(source="slot_index")
    instanceID = serializers.UUIDField(source="instance_id")
    itemID = serializers.IntegerField(source="item_id")
    itemAmount = serializers.IntegerField(source="item_amount")
    ratingPrice = serializers.IntegerField(source="rating_price")
    overallScore = serializers.FloatField(source="overall_score")
    itemBagName = serializers.CharField(source="item_bag_name")
    cookTime = serializers.IntegerField(source="cook_time")
    dishQuality = serializers.IntegerField(source="dish_quality")
    hideInShop = serializers.BooleanField(source="hide_in_shop")
    remainingUses = serializers.IntegerField(source="remaining_uses", required=False, default=-1)
    reviewComment = serializers.CharField(source="review_comment", required=False, allow_blank=True)
    batch = serializers.SerializerMethodField()

    def get_batch(self, instance):
        from game_time.models import GlobalGameTimeState
        from .batches import project_batch
        # A list serializer shares context, so resolve the catalog in one query.
        if "batch_items" not in self.context:
            rows = self.parent.instance if isinstance(self.parent, serializers.ListSerializer) else [instance]
            self.context["batch_items"] = Item.objects.in_bulk({row.item_id for row in rows if row.item_id})
        if "batch_minute" not in self.context:
            self.context["batch_minute"] = GlobalGameTimeState.objects.filter(pk=1).values_list("world_minute", flat=True).first()
        return project_batch(instance, self.context["batch_items"].get(instance.item_id), self.context["batch_minute"])

    class Meta:
        model = InventorySlot
        fields = ["slotIndex", "instanceID", "itemID", "itemAmount", "rated", "ratingPrice", "overallScore", "ingredient", "itemBagName", "cookTime", "dishQuality", "hideInShop", "remainingUses", "reviewComment", "batch"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get("remainingUses") is None:
            data["remainingUses"] = -1
        return data


def serialize_bag(bag):
    """Serialize an InventoryBag with all its slots (including empty ones)."""
    slots = bag.slots.all().order_by("slot_index")
    # 兜底：补全缺失的 item_bag_name
    dirty = []
    for slot in slots:
        if slot.item_id and not slot.item_bag_name:
            item = Item.objects.filter(item_id=slot.item_id).first()
            if item:
                slot.item_bag_name = f"{item.item_name}_{slot.slot_index}"
                dirty.append(slot)
    if dirty:
        InventorySlot.objects.bulk_update(dirty, ["item_bag_name"])
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


def serialize_npc_global_bag(bag):
    """Serialize an NpcGlobalInventory with all its slots."""
    from .models import NpcGlobalInventorySlot
    slots = bag.slots.all().order_by("slot_index")
    items = InventorySlotSerializer(slots, many=True).data
    return {"money": bag.money, "items": items}
