"""Durable communal merchant orders and one-visit capacity snapshots."""
import uuid

from django.db import models


class VillageCaravanOrder(models.Model):
    order_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    period_index = models.PositiveBigIntegerField(unique=True)
    # NULL releases the singleton without deleting the frozen order history.
    active_key = models.CharField(max_length=16, unique=True, null=True, blank=True, default="COMMUNAL")
    status = models.CharField(max_length=16, default="ANNOUNCED")
    announcement_world_minute = models.BigIntegerField()
    deadline_world_minute = models.BigIntegerField()
    budget_total = models.PositiveIntegerField()
    budget_spent = models.PositiveIntegerField(default=0)
    budget_source = models.CharField(max_length=40, default="EXTERNAL_MERCHANT_PROCUREMENT")
    rewards = models.JSONField(default=dict)
    procurement_snapshot = models.JSONField(default=dict)
    completed_world_minute = models.BigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(budget_spent__lte=models.F("budget_total")),
                                              name="caravan_order_budget_bounded")]


class VillageCaravanOrderLine(models.Model):
    order = models.ForeignKey("village.VillageCaravanOrder", on_delete=models.CASCADE, related_name="lines")
    item_id = models.PositiveIntegerField()
    target_quantity = models.PositiveIntegerField()
    delivered_quantity = models.PositiveIntegerField(default=0)
    min_quality = models.PositiveSmallIntegerField(default=1)
    quality_prices = models.JSONField(default=dict)

    class Meta:
        ordering = ["item_id"]
        constraints = [models.UniqueConstraint(fields=["order", "item_id"], name="caravan_order_item_once"),
            models.CheckConstraint(condition=models.Q(delivered_quantity__lte=models.F("target_quantity")),
                                   name="caravan_order_quantity_bounded")]


class VillageCaravanOrderWindow(models.Model):
    order = models.ForeignKey("village.VillageCaravanOrder", on_delete=models.CASCADE, related_name="windows")
    day_index = models.PositiveBigIntegerField()
    reserved_capacity = models.PositiveIntegerField()
    delivered_quantity = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["day_index"]
        constraints = [models.UniqueConstraint(fields=["order", "day_index"], name="caravan_order_window_once"),
            models.CheckConstraint(condition=models.Q(delivered_quantity__lte=models.F("reserved_capacity")),
                                   name="caravan_window_quantity_bounded")]


class VillageCaravanOrderReceipt(models.Model):
    request_id = models.UUIDField(primary_key=True)
    actor_type = models.CharField(max_length=8)
    actor_id = models.CharField(max_length=100)
    payload_hash = models.CharField(max_length=64)
    response = models.JSONField(default=dict)
    world_minute = models.BigIntegerField()


class VillageCaravanOrderDelivery(models.Model):
    receipt = models.OneToOneField("village.VillageCaravanOrderReceipt", on_delete=models.PROTECT)
    line = models.ForeignKey("village.VillageCaravanOrderLine", on_delete=models.PROTECT, related_name="deliveries")
    visit = models.ForeignKey("village.VillageCaravanVisit", on_delete=models.PROTECT, related_name="order_deliveries")
    actor_type = models.CharField(max_length=8)
    actor_id = models.CharField(max_length=100)
    source_instance_id = models.UUIDField()
    quantity = models.PositiveIntegerField()
    quality = models.PositiveSmallIntegerField()
    unit_price = models.PositiveIntegerField()
    total_income = models.PositiveIntegerField()
    batch_snapshot = models.JSONField(default=dict)
    world_minute = models.BigIntegerField()


class VillageCaravanOrderCompletion(models.Model):
    order = models.OneToOneField("village.VillageCaravanOrder", on_delete=models.CASCADE, related_name="completion")
    request_id = models.UUIDField(unique=True)
    world_minute = models.BigIntegerField()


class VillageCaravanReward(models.Model):
    completion = models.OneToOneField("village.VillageCaravanOrderCompletion", on_delete=models.CASCADE, related_name="reward")
    terms = models.JSONField(default=dict)
    earned_world_minute = models.BigIntegerField()
    status = models.CharField(max_length=16, default="PENDING")
    visit = models.ForeignKey("village.VillageCaravanVisit", null=True, blank=True, on_delete=models.PROTECT,
                              related_name="order_rewards")
    applied_world_minute = models.BigIntegerField(null=True, blank=True)


class VillageCaravanVisitAllocation(models.Model):
    visit = models.OneToOneField("village.VillageCaravanVisit", on_delete=models.CASCADE, primary_key=True,
                                 related_name="order_allocation")
    base_export_capacity = models.PositiveIntegerField()
    base_export_used = models.PositiveIntegerField(default=0)
    bonus_export_capacity = models.PositiveIntegerField(default=0)
    bonus_export_used = models.PositiveIntegerField(default=0)
    bonus_export_item_ids = models.JSONField(default=list)
    order_reserved_capacity = models.PositiveIntegerField(default=0)
    order_used = models.PositiveIntegerField(default=0)
    base_import_capacity = models.PositiveIntegerField()
    bonus_import_capacity = models.PositiveIntegerField(default=0)
    reward_snapshot = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(bonus_export_capacity__lte=20, bonus_import_capacity__lte=20),
                                   name="caravan_visit_reward_caps"),
            models.CheckConstraint(condition=models.Q(bonus_export_used__lte=models.F("bonus_export_capacity")),
                                   name="caravan_visit_bonus_bounded"),
            models.CheckConstraint(condition=models.Q(order_used__lte=models.F("order_reserved_capacity")),
                                   name="caravan_visit_order_bounded"),
        ]
