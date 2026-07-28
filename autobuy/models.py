from django.db import models

from account.models import GameAccount
from items.models import ItemType


class AutoBuyConfig(models.Model):
    enabled = models.BooleanField("启用自动购买", default=True)
    interval_minutes = models.IntegerField("执行间隔（分钟）", default=5, help_text="修改后需重启服务生效")
    fund = models.IntegerField("公共资金池", default=0, help_text="自动购买从此账户扣除金币")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "自动购买配置"
        verbose_name_plural = "自动购买配置"

    @classmethod
    def get_instance(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"自动购买配置 (启用={self.enabled}, 间隔={self.interval_minutes}分钟, 资金={self.fund})"


class AutoBuyRule(models.Model):
    buyer = models.ForeignKey(
        GameAccount, on_delete=models.CASCADE,
        related_name="autobuy_rules", verbose_name="买家账号",
    )
    item_types = models.JSONField(
        "物品类型", default=list,
        help_text="该买家将自动购买这些类型的所有低价物品",
    )
    max_price_multiplier = models.FloatField(
        "最高价格倍数", default=1.0,
        help_text="相对于公平价格的倍数，1.0表示只买低于公平价的",
    )
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "自动购买规则"
        verbose_name_plural = "自动购买规则"

    def get_item_types_display(self):
        type_map = dict(ItemType.choices)
        return ", ".join(type_map.get(t, t) for t in self.item_types)

    def __str__(self):
        return f"{self.buyer.username} 收 {self.get_item_types_display()}"


class AutoBuyPurchase(models.Model):
    config = models.ForeignKey(
        AutoBuyConfig, on_delete=models.CASCADE,
        related_name="purchases", verbose_name="配置",
    )
    buyer = models.ForeignKey(
        GameAccount, on_delete=models.CASCADE,
        related_name="autobuy_purchases", verbose_name="接收账号",
    )
    listing_id = models.IntegerField("挂单ID")
    item_id = models.IntegerField("物品ID")
    item_name = models.CharField("物品名称", max_length=100, default="")
    amount = models.IntegerField("数量")
    unit_price = models.IntegerField("单价")
    total_price = models.IntegerField("总价")
    commission = models.IntegerField("手续费", default=0)
    seller_earnings = models.IntegerField("卖家收入", default=0)
    fund_balance = models.IntegerField("购买后资金池余额")
    created_at = models.DateTimeField("购买时间", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "自动购买记录"
        verbose_name_plural = "自动购买记录"

    def __str__(self):
        return f"#{self.pk} {self.buyer.username} 购买 {self.item_name} x{self.amount} @{self.unit_price}"
