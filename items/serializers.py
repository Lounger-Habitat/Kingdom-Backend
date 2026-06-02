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
    iconUrl = serializers.URLField(source="icon_url", allow_null=True, required=False)
    iconImage = serializers.SerializerMethodField()
    isDeleted = serializers.BooleanField(source="is_deleted")

    class Meta:
        model = Item
        fields = [
            "itemID", "itemName", "itemType", "itemDescription",
            "itemUseRadius", "canPickedup", "canDropped", "canCarried",
            "itemPrice", "sellPercentage", "iconUrl", "iconImage", "isDeleted",
        ]

    def get_iconImage(self, obj):
        if obj.icon_image:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.icon_image.url)
            return obj.icon_image.url
        return None
