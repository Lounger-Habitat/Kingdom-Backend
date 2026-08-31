import uuid
from django.conf import settings
from django.db import models
from account.models import GameAccount
from inventory.models import NpcDefinition


class MarketListingSource(models.TextChoices):
    PLAYER = "player", "玩家"
    NPC = "npc", "NPC"
    SYSTEM = "system", "系统"


class MarketListingStatus(models.TextChoices):
    ACTIVE = "active", "出售中"
    SOLD_OUT = "sold_out", "已售罄"
    DELISTED = "delisted", "已下架"
    EXPIRED = "expired", "已过期"


class MarketListing(models.Model):
    source_type = models.CharField(
        max_length=10, choices=MarketListingSource.choices, default=MarketListingSource.PLAYER
    )
    seller = models.ForeignKey(
        GameAccount, on_delete=models.CASCADE,
        null=True, blank=True, related_name="market_listings",
    )
    seller_npc = models.ForeignKey(
        NpcDefinition, on_delete=models.CASCADE,
        null=True, blank=True, related_name="market_listings",
    )
    created_by_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name="创建管理员",
    )

    source_instance_id = models.UUIDField(null=True, blank=True, help_text="上架来源槽位实例ID快照")
    item_id = models.IntegerField(help_text="物品ID")
    item_name_snapshot = models.CharField(max_length=100, default="", blank=True)
    item_type_snapshot = models.CharField(max_length=30, default="", blank=True)
    icon_url_snapshot = models.TextField(default="", blank=True)

    total_amount = models.IntegerField(default=1)
    remaining_amount = models.IntegerField(default=1)
    price = models.IntegerField(help_text="单价")

    status = models.CharField(
        max_length=20, choices=MarketListingStatus.choices, default=MarketListingStatus.ACTIVE
    )

    item_snapshot = models.JSONField(default=dict, blank=True, help_text="完整物品实例属性快照")

    rated = models.BooleanField(default=False)
    rating_price = models.IntegerField(default=-1)
    overall_score = models.FloatField(default=0)
    ingredient = models.CharField(max_length=200, default="", blank=True)
    item_bag_name = models.CharField(max_length=100, default="", blank=True)
    cook_time = models.IntegerField(default=0)
    dish_quality = models.IntegerField(default=3)
    remaining_uses = models.IntegerField(null=True, blank=True)
    review_comment = models.TextField(default="", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="过期时间；系统挂单可为空表示不过期")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "市场挂单"
        verbose_name_plural = "市场挂单"
        indexes = [
            models.Index(fields=["source_type", "status"]),
            models.Index(fields=["status", "item_id"]),
            models.Index(fields=["seller", "status"]),
            models.Index(fields=["seller_npc", "status"]),
            models.Index(fields=["status", "price"]),
            models.Index(fields=["expires_at"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                name="market_listing_source_owner_valid",
                condition=(
                    (
                        models.Q(source_type=MarketListingSource.PLAYER)
                        & models.Q(seller__isnull=False)
                        & models.Q(seller_npc__isnull=True)
                    )
                    | (
                        models.Q(source_type=MarketListingSource.NPC)
                        & models.Q(seller__isnull=True)
                        & models.Q(seller_npc__isnull=False)
                    )
                    | (
                        models.Q(source_type=MarketListingSource.SYSTEM)
                        & models.Q(seller__isnull=True)
                        & models.Q(seller_npc__isnull=True)
                    )
                ),
            ),
        ]

    def __str__(self):
        if self.seller:
            seller_name = self.seller.username
        elif self.seller_npc:
            seller_name = self.seller_npc.name
        else:
            seller_name = "系统"
        return f"Listing #{self.pk} [{seller_name}] item={self.item_id} x{self.remaining_amount}/{self.total_amount} @{self.price}"


class MarketTransaction(models.Model):
    listing = models.ForeignKey(MarketListing, on_delete=models.CASCADE, related_name="transactions")
    source_type = models.CharField(max_length=10, choices=MarketListingSource.choices)
    seller = models.ForeignKey(
        GameAccount, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="market_sales",
    )
    seller_npc = models.ForeignKey(
        NpcDefinition, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="market_sales",
    )
    buyer = models.ForeignKey(
        GameAccount, on_delete=models.CASCADE,
        null=True, blank=True, related_name="market_purchases",
    )
    buyer_npc = models.ForeignKey(
        NpcDefinition, on_delete=models.CASCADE,
        null=True, blank=True, related_name="market_purchases",
    )
    item_id = models.IntegerField()
    item_name_snapshot = models.CharField(max_length=100, default="", blank=True)
    amount = models.IntegerField()
    unit_price = models.IntegerField()
    total_price = models.IntegerField()
    commission = models.IntegerField(default=0)
    seller_earnings = models.IntegerField(default=0)
    system_income = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "市场成交记录"
        verbose_name_plural = "市场成交记录"
        indexes = [
            models.Index(fields=["listing"]),
            models.Index(fields=["seller", "created_at"]),
            models.Index(fields=["seller_npc", "created_at"]),
            models.Index(fields=["buyer", "created_at"]),
            models.Index(fields=["buyer_npc", "created_at"]),
            models.Index(fields=["source_type", "created_at"]),
            models.Index(fields=["item_id", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                name="market_transaction_source_owner_valid",
                condition=(
                    (
                        models.Q(source_type=MarketListingSource.PLAYER)
                        & models.Q(seller__isnull=False)
                        & models.Q(seller_npc__isnull=True)
                    )
                    | (
                        models.Q(source_type=MarketListingSource.NPC)
                        & models.Q(seller__isnull=True)
                        & models.Q(seller_npc__isnull=False)
                    )
                    | (
                        models.Q(source_type=MarketListingSource.SYSTEM)
                        & models.Q(seller__isnull=True)
                        & models.Q(seller_npc__isnull=True)
                    )
                ),
            ),
            models.CheckConstraint(
                name="market_transaction_buyer_identity_valid",
                condition=(
                    (models.Q(buyer__isnull=False) & models.Q(buyer_npc__isnull=True))
                    | (models.Q(buyer__isnull=True) & models.Q(buyer_npc__isnull=False))
                ),
            ),
        ]

    def __str__(self):
        buyer_id = self.buyer_id or self.buyer_npc_id
        return f"Tx #{self.pk} listing={self.listing_id} {self.amount}x @{self.unit_price} -> {buyer_id}"
