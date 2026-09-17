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
    USABLE = "Usable", "可使用"


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
    use_effect_type = models.CharField(max_length=50, blank=True, default="", help_text="使用效果类型，如 unlock_recipe")
    use_effect_data = models.JSONField(default=dict, blank=True, help_text="使用效果数据，如 {\"recipeName\": \"红烧肉\"}")
    max_use_count = models.IntegerField(default=1, help_text="最大使用次数，-1 表示无限次")
    quality_enabled = models.BooleanField(default=False, help_text="普通物品是否区分品质；菜品始终保留品质")
    stackable = models.BooleanField(default=True, help_text="允许跨批次展示堆叠；菜品和有次数的道具仍独立展示")
    max_stack_size = models.PositiveIntegerField(null=True, blank=True, help_text="每个展示格的数量上限，留空表示不限；不改变实际库存批次")
    is_deleted = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["item_id"]
        constraints = [models.CheckConstraint(
            condition=models.Q(max_stack_size__isnull=True) | models.Q(max_stack_size__gt=0),
            name="item_positive_stack_size",
        )]
        verbose_name = "道具"
        verbose_name_plural = "道具"

    def save(self, *args, **kwargs):
        if not self.pk and self.item_type in (ItemType.FOOD, ItemType.USABLE):
            self.collectable = False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.item_id} - {self.item_name}"
