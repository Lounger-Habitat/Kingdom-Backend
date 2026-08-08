from django.core.exceptions import ValidationError
from django.db import models


class VillageLocationDefinition(models.Model):
    """Stable Backend definition for a Unity village location."""

    location_code = models.CharField(max_length=64, unique=True, db_index=True)
    display_name = models.CharField(max_length=100)
    scene_name = models.CharField(max_length=100, default="WuxiVillage")
    center_anchor_code = models.CharField(max_length=100, unique=True)
    location_type = models.CharField(max_length=50)
    bounds = models.JSONField(default=dict, blank=True)
    location_tags = models.JSONField(default=list, blank=True)
    opening_hours = models.JSONField(default=dict, blank=True)
    capacity = models.PositiveIntegerField(default=1)
    supported_action_types = models.JSONField(default=list, blank=True)
    allowed_occupations = models.JSONField(default=list, blank=True)
    required_story_states = models.JSONField(default=list, blank=True)
    is_public = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["location_code"]
        verbose_name = "村庄地点定义"
        verbose_name_plural = "村庄地点定义"

    def __str__(self):
        return f"{self.location_code} - {self.display_name}"


class NpcProfile(models.Model):
    """Low-frequency identity and capability configuration for one formal NPC."""

    npc = models.OneToOneField(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="village_profile",
    )
    display_name = models.CharField(max_length=100)
    occupation = models.CharField(max_length=100)
    home_location_code = models.CharField(max_length=64)
    work_location_code = models.CharField(max_length=64)
    social_location_code = models.CharField(max_length=64)
    personality_traits = models.JSONField(default=list, blank=True)
    personal_goal = models.TextField(default="", blank=True)
    capabilities = models.JSONField(default=list, blank=True)
    need_rates = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["npc__agent_id"]
        verbose_name = "NPC 村庄档案"
        verbose_name_plural = "NPC 村庄档案"

    def clean(self):
        super().clean()
        errors = {}
        for field_name in (
            "home_location_code",
            "work_location_code",
            "social_location_code",
        ):
            code = getattr(self, field_name, "")
            if code and not VillageLocationDefinition.objects.filter(
                location_code=code,
                is_active=True,
            ).exists():
                errors[field_name] = f"未知或未启用的地点代码: {code}"
        if self.npc_id and self.npc.is_test_only:
            errors["npc"] = "TestOnly NPC 不能绑定正式村庄档案"
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.display_name} ({self.npc.agent_id})"


class VillageSimulationConfig(models.Model):
    """Phase-gated village configuration. Phase zero seeds every flag as off."""

    config_key = models.CharField(max_length=32, unique=True, default="default")
    tick_interval_seconds = models.PositiveIntegerField(default=5)
    hub_command_timeout_seconds = models.PositiveIntegerField(default=30)
    command_queue_limit = models.PositiveIntegerField(default=8)
    day_start_minute = models.PositiveIntegerField(default=360)
    day_close_minute = models.PositiveIntegerField(default=1320)
    max_catchup_days = models.PositiveIntegerField(default=3)
    village_simulation_enabled = models.BooleanField(default=False)
    village_needs_enabled = models.BooleanField(default=False)
    village_economy_enabled = models.BooleanField(default=False)
    village_social_enabled = models.BooleanField(default=False)
    village_story_enabled = models.BooleanField(default=False)
    village_hub_command_adapter_enabled = models.BooleanField(default=False)
    village_offline_catchup_enabled = models.BooleanField(default=False)
    village_debug_overlay_enabled = models.BooleanField(default=False)
    debug_logs_enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "村庄模拟配置"
        verbose_name_plural = "村庄模拟配置"

    def __str__(self):
        return self.config_key
