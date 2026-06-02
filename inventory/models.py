import uuid
from django.db import models
from account.models import GameAccount
from items.models import Item


class InventoryBag(models.Model):
    player = models.ForeignKey(GameAccount, on_delete=models.CASCADE, null=True, blank=True)
    character_id = models.CharField(max_length=100)
    money = models.IntegerField(default=0)

    class Meta:
        unique_together = [("player", "character_id")]

    def __str__(self):
        return f"{self.character_id} (player={self.player_id}, money={self.money})"


class InventorySlot(models.Model):
    bag = models.ForeignKey(InventoryBag, on_delete=models.CASCADE, related_name="slots")
    slot_index = models.IntegerField()
    instance_id = models.UUIDField(default=uuid.uuid4, editable=False)
    item_id = models.IntegerField(default=0)
    item_amount = models.IntegerField(default=0)
    rated = models.BooleanField(default=False)
    rating_price = models.IntegerField(default=-1)
    overall_score = models.IntegerField(default=0, help_text="评价分数 0-100")
    ingredient = models.CharField(max_length=200, default="", blank=True)
    item_bag_name = models.CharField(max_length=100, default="", blank=True)
    cook_time = models.IntegerField(default=0, help_text="烹饪时长（秒）")
    dish_quality = models.IntegerField(default=3, help_text="品质：1略逊 2一般 3正常 4优秀 5极品 6绝品")

    class Meta:
        ordering = ["slot_index"]
        unique_together = [("bag", "slot_index")]

    def __str__(self):
        return f"{self.bag.character_id}[{self.slot_index}] item={self.item_id} x{self.item_amount}"


class BagTemplate(models.Model):
    """背包模板，用于新玩家初始化。"""
    character_id = models.CharField(max_length=100, unique=True, help_text="对应角色ID，如 player、tjplb")
    money = models.IntegerField(default=0, help_text="初始金币")

    def __str__(self):
        return f"模板: {self.character_id} (money={self.money})"


class BagTemplateSlot(models.Model):
    """背包模板中的物品格位。"""
    template = models.ForeignKey(BagTemplate, on_delete=models.CASCADE, related_name="slots")
    slot_index = models.IntegerField(default=-1)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, help_text="选择物品", null=True, blank=True)
    item_amount = models.IntegerField(default=1)
    rated = models.BooleanField(default=False)
    rating_price = models.IntegerField(default=-1)
    overall_score = models.IntegerField(default=0, help_text="评价分数 0-100")
    ingredient = models.CharField(max_length=200, default="", blank=True)
    cook_time = models.IntegerField(default=0, help_text="烹饪时长（秒）")
    dish_quality = models.IntegerField(default=3, help_text="品质：1略逊 2一般 3正常 4优秀 5极品 6绝品")

    class Meta:
        ordering = ["slot_index"]
        unique_together = [("template", "slot_index")]

    def save(self, *args, **kwargs):
        if self.slot_index < 0 and self.template_id:
            existing = set(
                BagTemplateSlot.objects.filter(template=self.template)
                .exclude(pk=self.pk)
                .values_list("slot_index", flat=True)
            )
            idx = 0
            while idx in existing:
                idx += 1
            self.slot_index = idx
        super().save(*args, **kwargs)

    def __str__(self):
        name = self.item.item_name if self.item else "?"
        return f"{self.template.character_id}[{self.slot_index}] {name} x{self.item_amount}"
