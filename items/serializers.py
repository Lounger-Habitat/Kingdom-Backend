from rest_framework import serializers
from .models import Item


class ItemSerializer(serializers.ModelSerializer):
    itemID = serializers.IntegerField(source="item_id")
    itemName = serializers.CharField(source="item_name")
    itemType = serializers.CharField(source="item_type")
    itemDescription = serializers.CharField(source="item_description")
    itemUseRadius = serializers.IntegerField(source="item_use_radius")
    canPickedup = serializers.BooleanField(source="can_picked_up")
    canDropped = serializers.BooleanField(source="can_dropped")
    canCarried = serializers.BooleanField(source="can_carried")
    itemPrice = serializers.IntegerField(source="item_price")
    sellPercentage = serializers.FloatField(source="sell_percentage")
    iconUrl = serializers.SerializerMethodField()
    iconImage = serializers.SerializerMethodField()
    useEffectType = serializers.CharField(source="use_effect_type")
    useEffectData = serializers.JSONField(source="use_effect_data")
    maxUseCount = serializers.IntegerField(source="max_use_count")
    isDeleted = serializers.BooleanField(source="is_deleted")
    inventoryRules = serializers.SerializerMethodField()

    def get_inventoryRules(self, obj):
        from inventory.batches import item_rules
        return item_rules(obj)

    class Meta:
        model = Item
        fields = [
            "itemID", "itemName", "itemType", "itemDescription",
            "itemUseRadius", "canPickedup", "canDropped", "canCarried",
            "itemPrice", "sellPercentage", "iconUrl", "iconImage",
            "useEffectType", "useEffectData", "maxUseCount", "isDeleted",
            "inventoryRules",
        ]

    def get_iconUrl(self, obj):
        # 优先用 icon_url，没有则兜底到 icon_image
        if obj.icon_url:
            return obj.icon_url
        if obj.icon_image:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.icon_image.url)
            return obj.icon_image.url
        return None

    def get_iconImage(self, obj):
        if obj.icon_image:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.icon_image.url)
            return obj.icon_image.url
        return None
