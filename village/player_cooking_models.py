"""One recoverable, server-timed pot per player; no inventory is reserved here."""
import uuid

from django.db import models


class VillagePlayerCookingSession(models.Model):
    player = models.OneToOneField("account.GameAccount", on_delete=models.CASCADE, primary_key=True)
    session_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    plan = models.JSONField(default=dict)
    state = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default="ACTIVE")
    started_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    settlement_request_id = models.UUIDField(null=True, blank=True)
