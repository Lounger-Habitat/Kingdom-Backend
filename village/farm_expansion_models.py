"""Persistent fixed field identity; crops and leases remain on VillageFarmPlot."""
from django.db import models


class VillageFarmExpansionSite(models.Model):
    site_code = models.CharField(max_length=32, primary_key=True)
    label = models.CharField(max_length=100)
    plot_codes = models.JSONField(default=list)
    location_code = models.CharField(max_length=64)
    preparation_affair = models.OneToOneField("VillageAffair", on_delete=models.PROTECT,
        related_name="farm_expansion_site")
    status = models.CharField(max_length=24, default="LOCKED")
    opened_world_minute = models.BigIntegerField(null=True, blank=True)
    completed_world_minute = models.BigIntegerField(null=True, blank=True)

    class Meta:
        app_label = "village"

