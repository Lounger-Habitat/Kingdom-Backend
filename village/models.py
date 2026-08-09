import uuid

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


# ============================================================================
# 阶段一：权威 Tick 与持久状态模型
# ============================================================================

class NpcRuntimeState(models.Model):
    """NPC 运行时状态（阶段一）。

    每个正式 NPC 一条记录，保存当前动作、位置、版本和恢复信息。
    """

    ACTION_STATUS_CHOICES = [
        ("IDLE", "空闲"),
        ("MOVING", "移动中"),
        ("ACTING", "行动中"),
        ("BLOCKED", "阻塞"),
        ("SLEEPING", "睡眠中"),
    ]

    GOAL_CHOICES = [
        ("NONE", "无目标"),
        ("MOVE", "移动"),
        ("WAIT", "等待"),
        ("GO_HOME", "回家"),
    ]

    ACTION_CHOICES = [
        ("", "无"),
        ("MOVE", "移动"),
        ("WAIT", "等待"),
        ("GO_HOME", "回家"),
    ]

    npc = models.OneToOneField(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="village_runtime",
    )
    current_location_code = models.CharField("当前地点", max_length=64, default="VILLAGE_SQUARE")
    current_point_code = models.CharField("当前点位", max_length=64, default="VILLAGE_SQUARE")
    current_goal = models.CharField("当前目标", max_length=32, choices=GOAL_CHOICES, default="NONE")
    current_action = models.CharField("当前动作", max_length=32, choices=ACTION_CHOICES, default="", blank=True)
    action_status = models.CharField("动作状态", max_length=16, choices=ACTION_STATUS_CHOICES, default="IDLE")
    action_started_world_minute = models.BigIntegerField("动作开始世界分钟", null=True, blank=True)
    action_ends_world_minute = models.BigIntegerField("动作结束世界分钟", null=True, blank=True)
    active_command_id = models.UUIDField("活动命令ID", null=True, blank=True)
    state_version = models.BigIntegerField("状态版本", default=0)
    last_simulated_world_minute = models.BigIntegerField("最后模拟世界分钟", default=0)
    time_revision = models.BigIntegerField("时间版本", default=0, help_text="状态最后确认时观察到的时钟 revision")
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "NPC 运行时状态"
        verbose_name_plural = "NPC 运行时状态"
        constraints = [
            models.CheckConstraint(
                name="npc_runtime_minute_non_negative",
                condition=models.Q(action_started_world_minute__gte=0) | models.Q(action_started_world_minute__isnull=True),
            ),
            models.CheckConstraint(
                name="npc_runtime_end_not_before_start",
                condition=models.Q(action_ends_world_minute__gte=models.F("action_started_world_minute"))
                | models.Q(action_ends_world_minute__isnull=True)
                | models.Q(action_started_world_minute__isnull=True),
            ),
        ]

    def __str__(self):
        return f"{self.npc.agent_id} - {self.action_status} (v{self.state_version})"


class NpcCommandState(models.Model):
    """NPC 命令状态（阶段一）。

    记录 Env Server 实际收到并处理的 Hub 指令。
    """

    STATUS_CHOICES = [
        ("RECEIVED", "已接收"),
        ("QUEUED", "已排队"),
        ("RUNNING", "执行中"),
        ("SUCCEEDED", "成功"),
        ("FAILED", "失败"),
        ("CANCELLED", "已取消"),
    ]

    QUEUE_MODE_CHOICES = [
        ("STRICT", "严格"),
    ]

    command_id = models.UUIDField("命令ID", primary_key=True, default=uuid.uuid4)
    message_id = models.UUIDField("消息ID", unique=True)
    request_id = models.UUIDField("请求ID", unique=True)
    npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="village_commands",
    )
    schema_version = models.CharField("协议版本", max_length=32, default="village.command.v1")
    sender_id = models.CharField("发送者ID", max_length=32)
    recipient_id = models.CharField("接收者ID", max_length=128)
    kind = models.CharField("命令类型", max_length=32)
    target_id = models.CharField("目标ID", max_length=64, blank=True, default="")
    location_code = models.CharField("地点码", max_length=64, blank=True, default="")
    sanitized_parameters = models.JSONField("清洗后参数", default=dict, blank=True)
    payload_sha256 = models.CharField("Payload SHA256", max_length=64)
    queue_mode = models.CharField("队列模式", max_length=16, choices=QUEUE_MODE_CHOICES, default="STRICT")
    status = models.CharField("状态", max_length=16, choices=STATUS_CHOICES, default="RECEIVED")

    # 时间字段
    sent_at_ms = models.BigIntegerField("发送时间戳(毫秒)", null=True, blank=True)
    received_at_ms = models.BigIntegerField("服务端接收时间戳(毫秒)", null=True, blank=True)
    ttl_ms = models.IntegerField("TTL(毫秒)", null=True, blank=True)
    not_before_world_minute = models.BigIntegerField("不早于世界分钟", null=True, blank=True)
    expires_world_minute = models.BigIntegerField("过期世界分钟", null=True, blank=True)
    duration_minutes = models.IntegerField("持续分钟", null=True, blank=True)

    # 生命周期时间
    received_world_minute = models.BigIntegerField("接收世界分钟", null=True, blank=True)
    started_world_minute = models.BigIntegerField("开始世界分钟", null=True, blank=True)
    completed_world_minute = models.BigIntegerField("完成世界分钟", null=True, blank=True)

    # 结果
    result_code = models.CharField("结果码", max_length=64, blank=True, default="")
    state_version = models.BigIntegerField("状态版本", null=True, blank=True)
    validated_at_state_version = models.BigIntegerField("校验时状态版本", null=True, blank=True)

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "NPC 命令状态"
        verbose_name_plural = "NPC 命令状态"
        indexes = [
            models.Index(fields=["npc", "status", "received_world_minute"], name="cmd_npc_status_wm"),
        ]

    def __str__(self):
        return f"{self.command_id} - {self.kind} - {self.status}"


class VillageActionReceipt(models.Model):
    """村庄动作收据（阶段一）。

    动作幂等结果，request_id 唯一。
    """

    SYSTEM_KIND_CHOICES = [
        ("TIME_ADVANCE", "时间推进"),
        ("VILLAGE_TICK", "村庄Tick"),
        ("CHECKPOINT", "检查点"),
    ]

    request_id = models.UUIDField("请求ID", primary_key=True, default=uuid.uuid4)
    command = models.OneToOneField(
        NpcCommandState,
        on_delete=models.CASCADE,
        related_name="receipt",
        null=True,
        blank=True,
    )
    npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="village_receipts",
        null=True,
        blank=True,
    )
    action_kind = models.CharField("动作类型", max_length=32)
    success = models.BooleanField("是否成功")
    result_code = models.CharField("结果码", max_length=64)
    response_data = models.JSONField("响应数据", default=dict, blank=True)
    world_minute = models.BigIntegerField("世界分钟")
    time_revision = models.BigIntegerField("时间版本")
    state_version = models.BigIntegerField("状态版本", null=True, blank=True)
    world_event = models.ForeignKey(
        "VillageWorldEvent",
        on_delete=models.SET_NULL,
        related_name="receipts",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "村庄动作收据"
        verbose_name_plural = "村庄动作收据"
        constraints = [
            models.UniqueConstraint(
                fields=["action_kind", "world_minute"],
                name="receipt_system_kind_wm_unique",
                condition=models.Q(action_kind__in=["TIME_ADVANCE", "VILLAGE_TICK"]),
            ),
        ]

    def __str__(self):
        return f"{self.request_id} - {self.action_kind} - {'OK' if self.success else 'FAIL'}"


class VillageWorldEvent(models.Model):
    """村庄世界事件（阶段一）。

    所有重要行为的事实记录，同一事实只记录一次。
    """

    VISIBILITY_CHOICES = [
        ("PUBLIC", "公开"),
        ("PRESENT", "在场者"),
        ("PRIVATE", "私密"),
        ("SYSTEM", "系统"),
    ]

    event_sequence = models.AutoField("事件序列", primary_key=True)
    event_id = models.UUIDField("事件ID", unique=True, default=uuid.uuid4)
    dedupe_key = models.CharField("去重键", max_length=128, unique=True)
    event_type = models.CharField("事件类型", max_length=64)
    world_minute = models.BigIntegerField("世界分钟")
    time_revision = models.BigIntegerField("时间版本")
    actor_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="village_events",
        null=True,
        blank=True,
    )
    participant_npc_ids = models.JSONField("参与者NPC ID列表", default=list, blank=True)
    location_code = models.CharField("地点码", max_length=64, blank=True, default="")
    point_code = models.CharField("点位码", max_length=64, blank=True, default="")
    command_id = models.UUIDField("命令ID", null=True, blank=True)
    request_id = models.UUIDField("请求ID", null=True, blank=True)
    cause_event_id = models.UUIDField("原因事件ID", null=True, blank=True)
    effects = models.JSONField("效果", default=dict, blank=True, help_text="阶段一必须为空")
    visibility = models.CharField("可见性", max_length=16, choices=VISIBILITY_CHOICES, default="PUBLIC")
    public_data = models.JSONField("公开数据", default=dict, blank=True)
    private_data = models.JSONField("私密数据", default=dict, blank=True)
    player_summary = models.CharField("玩家摘要", max_length=256, blank=True, default="")
    hub_summary = models.CharField("Hub摘要", max_length=256, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "村庄世界事件"
        verbose_name_plural = "村庄世界事件"
        ordering = ["event_sequence"]

    def __str__(self):
        return f"{self.event_sequence} - {self.event_type} (wm={self.world_minute})"


class NpcActionReservation(models.Model):
    """NPC 动作预约（阶段一）。

    地点、物品或交互对象的短期占用。
    """

    STATUS_CHOICES = [
        ("HELD", "持有"),
        ("COMMITTED", "已提交"),
        ("RELEASED", "已释放"),
        ("EXPIRED", "已过期"),
    ]

    reservation_id = models.UUIDField("预约ID", primary_key=True, default=uuid.uuid4)
    action_kind = models.CharField("动作类型", max_length=32)
    participants = models.ManyToManyField(
        "inventory.NpcDefinition",
        related_name="village_reservations",
        blank=True,
    )
    location_code = models.CharField("地点码", max_length=64, blank=True, default="")
    point_code = models.CharField("点位码", max_length=64, blank=True, default="")
    resource_key = models.CharField("资源键", max_length=128, blank=True, default="")
    active_lock_key = models.CharField("活动锁键", max_length=128, unique=True, null=True, blank=True)
    status = models.CharField("状态", max_length=16, choices=STATUS_CHOICES, default="HELD")
    acquired_world_minute = models.BigIntegerField("获取世界分钟")
    expires_world_minute = models.BigIntegerField("过期世界分钟")
    released_world_minute = models.BigIntegerField("释放世界分钟", null=True, blank=True)
    command_id = models.UUIDField("命令ID", null=True, blank=True)
    request_id = models.UUIDField("请求ID", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "NPC 动作预约"
        verbose_name_plural = "NPC 动作预约"

    def __str__(self):
        return f"{self.reservation_id} - {self.action_kind} - {self.status}"


class VillageDeliveryOutbox(models.Model):
    """村庄投递发件箱（阶段一）。

    待发送给 Hub 的最终 outcome 与事件投递记录。
    使用至少一次投递和接收方去重。
    """

    STATUS_CHOICES = [
        ("PENDING", "待发送"),
        ("IN_FLIGHT", "发送中"),
        ("ACKED", "已确认"),
        ("DEAD_LETTER", "死信"),
    ]

    MESSAGE_KIND_CHOICES = [
        ("OUTCOME", "结果"),
        ("EVENT", "事件"),
    ]

    delivery_id = models.UUIDField("投递ID", primary_key=True, default=uuid.uuid4)
    dedupe_key = models.CharField("去重键", max_length=128, unique=True)
    recipient = models.CharField("接收者", max_length=32)
    message_kind = models.CharField("消息类型", max_length=16, choices=MESSAGE_KIND_CHOICES)
    command = models.ForeignKey(
        NpcCommandState,
        on_delete=models.CASCADE,
        related_name="outbox_entries",
        null=True,
        blank=True,
    )
    receipt = models.ForeignKey(
        VillageActionReceipt,
        on_delete=models.CASCADE,
        related_name="outbox_entries",
        null=True,
        blank=True,
    )
    event = models.ForeignKey(
        VillageWorldEvent,
        on_delete=models.CASCADE,
        related_name="outbox_entries",
        null=True,
        blank=True,
    )
    payload = models.JSONField("完整载荷")
    priority = models.IntegerField("优先级", default=0)
    status = models.CharField("状态", max_length=16, choices=STATUS_CHOICES, default="PENDING")
    attempt_count = models.IntegerField("尝试次数", default=0)
    next_attempt_at = models.DateTimeField("下次尝试时间", null=True, blank=True)
    lease_until = models.DateTimeField("租约截止", null=True, blank=True)
    last_sent_at = models.DateTimeField("最后发送时间", null=True, blank=True)
    acked_at = models.DateTimeField("确认时间", null=True, blank=True)
    last_error_code = models.CharField("最后错误码", max_length=64, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "村庄投递发件箱"
        verbose_name_plural = "村庄投递发件箱"
        ordering = ["-priority", "created_at"]

    def __str__(self):
        return f"{self.delivery_id} - {self.message_kind} - {self.status}"
