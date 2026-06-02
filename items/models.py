from django.db import models


class ItemType(models.TextChoices):
    SEED = "Seed", "种子"
    COMMODITY = "Commodity", "商品"
    FURNITURE = "Furniture", "家具"
    HOE_TOOL = "HoeTool", "锄头"
    CHOP_TOOL = "ChopTool", "斧头"
    BREAK_TOOL = "BreakTool", "锤子"
    REAP_TOOL = "ReapTool", "镰刀"
    WATER_TOOL = "WaterTool", "水壶"
    COLLECT_TOOL = "CollectTool", "采集工具"
    REAPABLE_SCENERY = "ReapableScenery", "可收割场景"
    FOOD = "食物", "食物"
    INGREDIENT = "食材", "食材"
    MEAT = "肉类", "肉类"
    VEGETABLE = "蔬菜", "蔬菜"
    FRUIT = "水果", "水果"
    SEASONING = "调料类", "调料类"


class Item(models.Model):
    item_id = models.IntegerField(unique=True, primary_key=True)
    item_name = models.CharField(max_length=100)
    item_type = models.CharField(max_length=20, choices=ItemType.choices, default=ItemType.COMMODITY)
    item_description = models.TextField(blank=True, default="")
    item_use_radius = models.IntegerField(default=0)
    can_picked_up = models.BooleanField(default=False)
    can_dropped = models.BooleanField(default=False)
    can_carried = models.BooleanField(default=False)
    item_price = models.IntegerField(default=0)
    sell_percentage = models.FloatField(default=0.5)
    icon_url = models.URLField(null=True, blank=True)
    icon_image = models.ImageField(upload_to="items/icons/", null=True, blank=True)
    collectable = models.BooleanField(default=True, help_text="是否可被采集")
    collect_weight = models.IntegerField(default=100, help_text="采集权重，越大越容易被抽到")
    collect_min_amount = models.IntegerField(default=1, help_text="采集时最少获得数量")
    collect_max_amount = models.IntegerField(default=2, help_text="采集时最多获得数量")
    is_deleted = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["item_id"]

    def save(self, *args, **kwargs):
        if not self.pk and self.item_type == ItemType.FOOD:
            self.collectable = False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.item_id} - {self.item_name}"
