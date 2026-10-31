"""Small persistent records for authored career milestones and real evidence."""
from django.db import models


class VillageCareerDevelopment(models.Model):
    code = models.CharField(max_length=48, primary_key=True)
    owner_npc_id = models.CharField(max_length=32)
    owner_name = models.CharField(max_length=40)
    title = models.CharField(max_length=120)
    phase = models.PositiveIntegerField(default=1)
    location_code = models.CharField(max_length=64)
    preparation_affair = models.OneToOneField("VillageAffair", null=True, blank=True,
        on_delete=models.PROTECT, related_name="career_development")
    status = models.CharField(max_length=16, default="LOCKED")
    progress = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    opened_world_minute = models.BigIntegerField(null=True, blank=True)
    trial_world_minute = models.BigIntegerField(null=True, blank=True)
    completed_world_minute = models.BigIntegerField(null=True, blank=True)

    class Meta:
        app_label = "village"
        ordering = ["owner_npc_id", "phase", "code"]


class VillageCareerEvidence(models.Model):
    kind = models.CharField(max_length=16)
    request_id = models.CharField(max_length=128)
    actor_type = models.CharField(max_length=8)
    actor_id = models.CharField(max_length=100)
    items = models.JSONField(default=dict)
    metadata = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    world_minute = models.BigIntegerField()

    class Meta:
        app_label = "village"
        constraints = [models.UniqueConstraint(fields=["kind", "request_id", "actor_type", "actor_id"], name="career_evidence_once")]


class VillageCareerProductionReservation(models.Model):
    request_id = models.CharField(max_length=128)
    item_id = models.PositiveIntegerField()
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=16, default="RESERVED")
    world_minute = models.BigIntegerField()
    settled_world_minute = models.BigIntegerField(null=True, blank=True)

    class Meta:
        app_label = "village"
        constraints = [models.UniqueConstraint(fields=["request_id", "item_id"], name="career_trial_reservation_once")]
