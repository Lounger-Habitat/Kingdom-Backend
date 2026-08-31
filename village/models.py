import uuid

from django.core.exceptions import ValidationError
from django.db import models

from .phase3_contract import (
    ListingStatus,
    PriceMode,
    StoreOperationalStatus,
    TradeOfferStatus,
)
from .phase5_contract import StoryOutcome, StoryPhase, StoryPlan


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


class NpcSubjectiveGoalState(models.Model):
    """Agent-authored current goal, separate from profile and action goals."""

    STATUS_CHOICES = [
        ("ACTIVE", "进行中"),
        ("COMPLETED", "已完成"),
        ("ABANDONED", "已放弃"),
    ]

    npc = models.OneToOneField(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="subjective_goal_state",
    )
    goal_id = models.UUIDField("目标ID", null=True, blank=True)
    summary = models.CharField("目标摘要", max_length=120, blank=True, default="")
    status = models.CharField("状态", max_length=16, choices=STATUS_CHOICES, default="ABANDONED")
    visibility = models.CharField("可见性", max_length=16, default="PUBLIC")
    goal_version = models.BigIntegerField("目标版本", default=0)
    updated_world_minute = models.BigIntegerField("更新世界分钟", default=0)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "NPC 主观目标"
        verbose_name_plural = "NPC 主观目标"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(goal_version__gte=0, updated_world_minute__gte=0),
                name="npc_subjective_goal_versions_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.npc.agent_id}: {self.status} v{self.goal_version}"


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
    village_direct_trade_enabled = models.BooleanField(default=False)
    village_social_enabled = models.BooleanField(default=False)
    village_story_enabled = models.BooleanField(default=False)
    village_hub_command_adapter_enabled = models.BooleanField(default=False)
    village_offline_catchup_enabled = models.BooleanField(default=False)
    village_presentation_enabled = models.BooleanField(default=False)
    village_debug_overlay_enabled = models.BooleanField(default=False)
    debug_logs_enabled = models.BooleanField(default=False)
    story_fixed_seed = models.CharField(max_length=128, default="village-story-v1")
    story_repair_fund_target = models.PositiveIntegerField(default=500)
    story_repair_wood_target = models.PositiveIntegerField(default=10)
    story_repair_metal_target = models.PositiveIntegerField(default=4)
    story_repair_labor_target = models.PositiveIntegerField(default=24)
    story_public_trust_minimum = models.SmallIntegerField(default=20)
    story_latest_day = models.PositiveIntegerField(default=25)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "村庄模拟配置"
        verbose_name_plural = "村庄模拟配置"

    def __str__(self):
        return self.config_key


class VillageActionDurationConfig(models.Model):
    """Backend-managed durations for every executable village action."""

    ACTION_METADATA = {
        "MOVE": ("阶段一", "移动", "前往指定地点；以寻路到达为准，仅“导航超时（秒）”生效。"),
        "WAIT": ("阶段一", "等待", "原地等待；使用“时长（世界分钟）”。"),
        "GO_HOME": ("阶段一", "回家", "返回 NPC 住所；以寻路到达为准，仅“导航超时（秒）”生效。"),
        "EAT": ("阶段二", "进食", "进食并结算饥饿值；时长固定为 30 个世界分钟。"),
        "REST": ("阶段二", "休息", "在家或餐厅休息；30–120 个世界分钟，且必须为 30 的倍数。"),
        "SLEEP": ("阶段二", "睡眠", "在家睡眠；30–600 个世界分钟，且必须为 30 的倍数。"),
        "SOCIALIZE": ("阶段二", "社交", "与另一名 NPC 社交；30–120 个世界分钟，且必须为 30 的倍数。"),
        "WORK_PLACEHOLDER": ("阶段二", "工作", "在工作地点执行工作；30–240 个世界分钟，且必须为 30 的倍数。"),
        "PLANT": ("阶段三", "播种", "在 Bill 农田播种；使用“时长（世界分钟）”。"),
        "HARVEST": ("阶段三", "收获", "在 Bill 农田收获；使用“时长（世界分钟）”。"),
        "HUNT": ("阶段三", "狩猎", "在狩猎入口狩猎；使用“时长（世界分钟）”。"),
        "COOK": ("阶段三", "烹饪", "在餐厅烹饪；使用“时长（世界分钟）”。"),
        "CREATE_ORDER": ("阶段三", "创建订单", "创建交易订单；使用“时长（世界分钟）”。"),
        "CANCEL_ORDER": ("阶段三", "取消订单", "取消自己的开放订单；使用“时长（世界分钟）”。"),
        "START_DELIVERY": ("阶段三", "开始配送", "开始已匹配订单的配送；使用“时长（世界分钟）”。"),
        "IMPORT_GOODS": ("阶段三", "外部进货", "在贸易道路向商队进货；使用“时长（世界分钟）”。"),
        "EXPORT_GOODS": ("阶段三", "外部出货", "在贸易道路向商队出货；使用“时长（世界分钟）”。"),
        "RATE_DISH": ("阶段三", "菜品评分", "在档案馆为菜品评分；使用“时长（世界分钟）”。"),
        "PUBLISH_PRICE_INDEX": ("阶段三", "发布价格指数", "在档案馆发布当日价格指数；使用“时长（世界分钟）”。"),
        "AUDIT_PUBLIC_FUND": ("阶段三", "审计公款", "在档案馆审计公共资金；使用“时长（世界分钟）”。"),
        "PROPOSE_TRADE": ("阶段三", "发起交易报价", "向指定 NPC 发起结构化定向报价；使用“时长（世界分钟）”。"),
        "RESPOND_TRADE": ("阶段三", "响应交易报价", "接受或拒绝交易报价；使用“时长（世界分钟）”。"),
        "CANCEL_TRADE_OFFER": ("阶段三", "取消交易报价", "发起方取消待处理报价；使用“时长（世界分钟）”。"),
        "LIST_STORE_ITEM": ("阶段三", "上架商品", "经营 NPC 将个人物品转入商店并上架；使用“时长（世界分钟）”。"),
        "UPDATE_STORE_PRICE": ("阶段三", "修改商品价格", "经营 NPC 修改上架商品单价；使用“时长（世界分钟）”。"),
        "DELIST_STORE_ITEM": ("阶段三", "下架商品", "经营 NPC 下架商品并取回库存；使用“时长（世界分钟）”。"),
        "BUY_FROM_STORE": ("阶段三", "商店购买", "其他 NPC 按上架记录价格购买商品；使用“时长（世界分钟）”。"),
        "BUY_FROM_MARKETPLACE": ("阶段三", "购买全服商品", "在市场街购买全服 Marketplace 挂单；使用“时长（世界分钟）”。"),
        "SELL_ON_MARKETPLACE": ("阶段三", "上架全服商品", "在市场街将个人库存上架到全服 Marketplace；使用“时长（世界分钟）”。"),
        "DELIST_FROM_MARKETPLACE": ("阶段三", "下架全服商品", "在市场街下架自己的全服 Marketplace 挂单；使用“时长（世界分钟）”。"),
        "BROWSE_MARKETPLACE": ("查询", "浏览全服市场", "只读查询，不进入经济结算。"),
        "CHECK_MY_MARKETPLACE_LISTINGS": ("查询", "查看我的全服挂单", "只读查询，不进入经济结算。"),
        "CHECK_MARKETPLACE_HISTORY": ("查询", "查看全服成交历史", "只读查询，不进入经济结算。"),
        "INSPECT_STORE_INVENTORY": ("阶段三", "查看商店库存", "只读查询，不进入经济结算。"),
        "JOINT_WORK": ("阶段四", "共同工作", "与另一名 NPC 在指定地点共同工作；固定使用 120 个世界分钟。"),
        "MEETING": ("阶段四", "见面", "与另一名 NPC 在指定地点会面；固定使用 30 个世界分钟。"),
        "CREATE_COMMITMENT": ("阶段四", "创建承诺", "创建结构化 NPC 承诺；使用 1 个世界分钟。"),
        "CANCEL_COMMITMENT": ("阶段四", "取消承诺", "取消仍开放的 NPC 承诺；使用 1 个世界分钟。"),
        "PERFORM_COMMITMENT": ("阶段四", "履行承诺", "履行短时承诺；共同工作和见面使用各自固定时长。"),
    }
    ACTION_CHOICES = [(action, f"{label}（{action}）") for action, (_, label, _) in ACTION_METADATA.items()]
    NAVIGATION_ACTIONS = frozenset({"MOVE", "GO_HOME"})
    PHASE2_DURATION_LIMITS = {
        "EAT": (30, 30), "REST": (30, 120), "SLEEP": (30, 600),
        "SOCIALIZE": (30, 120), "WORK_PLACEHOLDER": (30, 240),
    }
    DEFAULT_DURATIONS = {
        "WAIT": 1,
        "EAT": 30,
        "REST": 30,
        "SLEEP": 30,
        "SOCIALIZE": 30,
        "WORK_PLACEHOLDER": 30,
        "PLANT": 60,
        "HARVEST": 90,
        "HUNT": 120,
        "COOK": 90,
        "CREATE_ORDER": 1,
        "CANCEL_ORDER": 1,
        "START_DELIVERY": 1,
        "IMPORT_GOODS": 120,
        "EXPORT_GOODS": 120,
        "RATE_DISH": 30,
        "PUBLISH_PRICE_INDEX": 30,
        "AUDIT_PUBLIC_FUND": 30,
        "PROPOSE_TRADE": 1,
        "RESPOND_TRADE": 1,
        "CANCEL_TRADE_OFFER": 1,
        "LIST_STORE_ITEM": 1,
        "UPDATE_STORE_PRICE": 1,
        "DELIST_STORE_ITEM": 1,
        "BUY_FROM_STORE": 1,
        "BUY_FROM_MARKETPLACE": 1,
        "SELL_ON_MARKETPLACE": 1,
        "DELIST_FROM_MARKETPLACE": 1,
        "BROWSE_MARKETPLACE": 5,
        "CHECK_MY_MARKETPLACE_LISTINGS": 5,
        "CHECK_MARKETPLACE_HISTORY": 5,
        "INSPECT_STORE_INVENTORY": 5,
        "JOINT_WORK": 120,
        "MEETING": 30,
        "CREATE_COMMITMENT": 1,
        "CANCEL_COMMITMENT": 1,
        "PERFORM_COMMITMENT": 1,
    }
    DEFAULT_NAVIGATION_TIMEOUT_SECONDS = {
        "MOVE": 180,
        "GO_HOME": 180,
    }

    action_name = models.CharField("动作", max_length=32, unique=True, choices=ACTION_CHOICES)
    duration_minutes = models.PositiveIntegerField("时长（世界分钟）", null=True, blank=True)
    navigation_timeout_seconds = models.PositiveIntegerField("导航超时（秒）", null=True, blank=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "村庄动作时长配置"
        verbose_name_plural = "村庄动作时长配置"
        ordering = ["action_name"]
        constraints = [
            models.CheckConstraint(
                name="village_action_duration_positive",
                condition=models.Q(duration_minutes__isnull=True) | models.Q(duration_minutes__gt=0),
            ),
            models.CheckConstraint(
                name="village_navigation_timeout_positive",
                condition=models.Q(navigation_timeout_seconds__isnull=True) | models.Q(navigation_timeout_seconds__gt=0),
            ),
        ]

    @classmethod
    def default_durations(cls):
        return dict(cls.DEFAULT_DURATIONS)

    @classmethod
    def default_navigation_timeouts(cls):
        return dict(cls.DEFAULT_NAVIGATION_TIMEOUT_SECONDS)

    @classmethod
    def metadata(cls, action_name):
        return cls.ACTION_METADATA.get(action_name, ("未知阶段", action_name, ""))

    def clean(self):
        super().clean()
        if self.action_name in self.NAVIGATION_ACTIONS:
            if not self.navigation_timeout_seconds:
                raise ValidationError({"navigation_timeout_seconds": "移动和回家必须设置大于 0 的导航超时秒数。"})
            return
        if not self.duration_minutes:
            raise ValidationError({"duration_minutes": "此动作必须设置大于 0 的世界分钟时长。"})
        limits = self.PHASE2_DURATION_LIMITS.get(self.action_name)
        if limits and (self.duration_minutes % 30 or not limits[0] <= self.duration_minutes <= limits[1]):
            raise ValidationError({
                "duration_minutes": f"{self.action_name} 必须在 {limits[0]}–{limits[1]} 之间，且为 30 的倍数。"
            })

    def __str__(self):
        value = self.navigation_timeout_seconds if self.action_name in self.NAVIGATION_ACTIONS else self.duration_minutes
        unit = "秒" if self.action_name in self.NAVIGATION_ACTIONS else "世界分钟"
        return f"{self.action_name}: {value}{unit}"


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
    # Phase two needs.  These are deliberately kept on the phase-one runtime
    # row so the mutation lane can lock one authoritative record per NPC.
    # Public projections divide the fixed-point values by NEEDS_SCALE (10).
    hunger_fp = models.IntegerField("饥饿 fixed-point", default=200)
    energy_fp = models.IntegerField("精力 fixed-point", default=800)
    health_fp = models.IntegerField("健康 fixed-point", default=1000)
    social_fp = models.IntegerField("社交 fixed-point", default=200)
    stress_fp = models.IntegerField("压力 fixed-point", default=100)
    hunger_remainder = models.IntegerField("饥饿余数", default=0)
    energy_remainder = models.IntegerField("精力余数", default=0)
    health_remainder = models.IntegerField("健康余数", default=0)
    social_remainder = models.IntegerField("社交余数", default=0)
    stress_remainder = models.IntegerField("压力余数", default=0)
    needs_world_minute = models.BigIntegerField("需求推进世界分钟", default=0)
    away_until_world_minute = models.BigIntegerField("撤离返回世界分钟", null=True, blank=True)
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
            models.CheckConstraint(
                name="npc_runtime_needs_fp_in_range",
                condition=(
                    models.Q(hunger_fp__gte=0, hunger_fp__lte=1000)
                    & models.Q(energy_fp__gte=0, energy_fp__lte=1000)
                    & models.Q(health_fp__gte=0, health_fp__lte=1000)
                    & models.Q(social_fp__gte=0, social_fp__lte=1000)
                    & models.Q(stress_fp__gte=0, stress_fp__lte=1000)
                ),
            ),
            models.CheckConstraint(
                name="npc_runtime_needs_remainder_in_range",
                condition=(
                    models.Q(hunger_remainder__gte=-29, hunger_remainder__lte=29)
                    & models.Q(energy_remainder__gte=-29, energy_remainder__lte=29)
                    & models.Q(health_remainder__gte=-29, health_remainder__lte=29)
                    & models.Q(social_remainder__gte=-29, social_remainder__lte=29)
                    & models.Q(stress_remainder__gte=-29, stress_remainder__lte=29)
                ),
            ),
            models.CheckConstraint(
                name="npc_runtime_needs_minute_non_negative",
                condition=models.Q(needs_world_minute__gte=0),
            ),
        ]

    def __str__(self):
        return f"{self.npc.agent_id} - {self.action_status} (v{self.state_version})"


class NpcNeedsDaySummary(models.Model):
    """One authoritative daily needs summary per NPC and absolute day."""

    npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="needs_day_summaries",
    )
    day_index = models.BigIntegerField()
    sleep_minutes = models.PositiveIntegerField(default=0)
    successful_meals = models.PositiveIntegerField(default=0)
    severe_hunger_minutes = models.PositiveIntegerField(default=0)
    injured = models.BooleanField(default=False)
    finalized = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "NPC 每日需求摘要"
        verbose_name_plural = "NPC 每日需求摘要"
        constraints = [
            models.UniqueConstraint(
                fields=["npc", "day_index"],
                name="npc_needs_day_summary_unique",
            ),
            models.CheckConstraint(
                name="npc_needs_day_index_non_negative",
                condition=models.Q(day_index__gte=0),
            ),
        ]

    def __str__(self):
        return f"{self.npc.agent_id} day={self.day_index}"


class NpcSocialDailyCounter(models.Model):
    """Daily count for a pair canonicalized by the service's stable agent_id order."""

    low_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="social_daily_counters_as_low",
    )
    high_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="social_daily_counters_as_high",
    )
    day_index = models.BigIntegerField()
    socialize_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "NPC 每日社交计数"
        verbose_name_plural = "NPC 每日社交计数"
        constraints = [
            models.UniqueConstraint(
                fields=["low_npc", "high_npc", "day_index"],
                name="npc_social_day_counter_unique",
            ),
            models.CheckConstraint(
                name="npc_social_day_index_non_negative",
                condition=models.Q(day_index__gte=0),
            ),
        ]

    def __str__(self):
        return f"{self.low_npc.agent_id}/{self.high_npc.agent_id} day={self.day_index}"


class VillageCommunityMealDailyUsage(models.Model):
    """Locked per-day community meal quota, including each NPC's use count."""

    day_index = models.BigIntegerField(unique=True)
    total_meals = models.PositiveIntegerField(default=0)
    npc_usage = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "村庄社区餐每日用量"
        verbose_name_plural = "村庄社区餐每日用量"
        constraints = [
            models.CheckConstraint(
                name="community_meal_day_index_non_negative",
                condition=models.Q(day_index__gte=0),
            ),
            models.CheckConstraint(
                name="community_meal_total_at_most_five",
                condition=models.Q(total_meals__gte=0, total_meals__lte=5),
            ),
        ]

    def __str__(self):
        return f"community meals day={self.day_index}: {self.total_meals}/5"


class NpcCommandState(models.Model):
    """NPC 命令状态（阶段一）。

    记录 Env Server 实际收到并处理的 Hub 指令。
    """

    STATUS_CHOICES = [
        ("RECEIVED", "已接收"),
        ("QUEUED", "已排队"),
        ("RUNNING", "执行中"),
        ("SETTLING", "结算中"),
        ("SUCCEEDED", "成功"),
        ("FAILED", "失败"),
        ("CANCELLED", "已取消"),
        ("EXPIRED", "已过期"),
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
        ("PARTICIPANTS", "参与者"),
        ("PRESENT", "在场者"),
        ("PLAYER_PRIVATE", "玩家私密"),
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
    witness_npc_ids = models.JSONField("见证者NPC ID列表", default=list, blank=True)
    player_ids = models.JSONField("指定玩家ID列表", default=list, blank=True)
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


class NpcDialogueSession(models.Model):
    """Authoritative transcript and settlement anchor for one NPC dialogue."""

    STATUS_CHOICES = [
        ("ACTIVE", "进行中"),
        ("COMPLETED", "已完成"),
        ("EXPIRED", "已过期"),
    ]

    conversation_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    initiator = models.ForeignKey(
        "inventory.NpcDefinition", on_delete=models.CASCADE, related_name="initiated_dialogues"
    )
    target = models.ForeignKey(
        "inventory.NpcDefinition", on_delete=models.CASCADE, related_name="received_dialogues"
    )
    reservation = models.OneToOneField(
        NpcActionReservation, on_delete=models.PROTECT, related_name="dialogue_session"
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="ACTIVE")
    topic = models.CharField(max_length=120, blank=True, default="")
    messages = models.JSONField(default=list, blank=True)
    end_requests = models.JSONField(default=list, blank=True)
    started_world_minute = models.BigIntegerField()
    expires_world_minute = models.BigIntegerField()
    ended_world_minute = models.BigIntegerField(null=True, blank=True)
    end_reason = models.CharField(max_length=64, blank=True, default="")
    valid_for_social_needs = models.BooleanField(null=True, blank=True)
    social_multiplier_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "NPC 对话会话"
        verbose_name_plural = "NPC 对话会话"

    def __str__(self):
        return f"{self.conversation_id} - {self.status}"


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


class VillageDeliveryCursor(models.Model):
    """Per-recipient event cursor persisted with delivery acknowledgements."""

    recipient = models.CharField("接收者", max_length=32, primary_key=True)
    last_event_sequence = models.PositiveBigIntegerField("最后事件序列", default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "村庄投递游标"
        verbose_name_plural = "村庄投递游标"


# ============================================================================
# Phase 3: Economy models
# ============================================================================


class EconomicLedgerEntry(models.Model):
    """Economic ledger entry recording every asset mutation in Phase 3."""

    request_id = models.UUIDField("请求ID", unique=True)
    entry_type = models.CharField("类型", max_length=32)
    # PRODUCTION, TRADE, CONSUMPTION, IMPORT, EXPORT, PUBLIC_FUND,
    # SPOILAGE, TRAVELER_SALE, SALARY, CARAVAN, etc.
    actor_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    changes = models.JSONField("变更", default=dict)
    # {npc_id: {item_id: delta, money: delta}, ...}
    external_delta = models.IntegerField("外部差额", default=0)
    # positive=external income, negative=external expense
    world_minute = models.BigIntegerField("世界分钟")
    event = models.ForeignKey(
        VillageWorldEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "经济账目"
        verbose_name_plural = "经济账目"

    def __str__(self):
        return f"{self.entry_type} request={self.request_id} wm={self.world_minute}"


class VillageAccount(models.Model):
    """Village-level account (e.g. PUBLIC_FUND)."""

    account_code = models.CharField("账户代码", max_length=32, unique=True)
    balance = models.IntegerField("余额", default=0)
    asset_version = models.BigIntegerField("资产版本", default=0)
    last_caravan_day = models.IntegerField("上次商队日", null=True, blank=True)
    last_closed_day_index = models.IntegerField("上次关闭日索引", default=-1)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "村庄账户"
        verbose_name_plural = "村庄账户"
        constraints = [
            models.CheckConstraint(
                name="village_account_balance_non_negative",
                condition=models.Q(balance__gte=0),
            ),
        ]

    def __str__(self):
        return f"{self.account_code} balance={self.balance}"


class VillageFarmPlot(models.Model):
    """A single farm plot on the village."""

    plot_code = models.CharField("地块代码", max_length=32, unique=True)
    status = models.CharField("状态", max_length=16, default="EMPTY")
    # EMPTY / GROWING / MATURE
    batch_id = models.UUIDField("批次ID", null=True, blank=True, unique=True)
    crop_code = models.CharField("作物代码", max_length=32, default="", blank=True)
    planted_world_minute = models.BigIntegerField("播种世界分钟", null=True, blank=True)
    mature_world_minute = models.BigIntegerField("成熟世界分钟", null=True, blank=True)
    yield_seed = models.IntegerField("预期产量种子", null=True, blank=True)
    source_request_id = models.UUIDField("来源请求ID", null=True, blank=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "农田地块"
        verbose_name_plural = "农田地块"

    def __str__(self):
        return f"{self.plot_code} status={self.status} crop={self.crop_code}"


class NpcTradeOrder(models.Model):
    """An NPC trade order (BUY or SELL)."""

    order_id = models.UUIDField("订单ID", primary_key=True, default=uuid.uuid4)
    request_id = models.UUIDField("请求ID", unique=True)
    side = models.CharField("方向", max_length=4)  # BUY / SELL
    npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="trade_orders",
    )
    item_id = models.IntegerField("物品ID")
    amount = models.IntegerField("数量")
    unit_price = models.IntegerField("单价")
    delivery_point_code = models.CharField("交付点", max_length=64)
    expires_world_minute = models.BigIntegerField("过期世界分钟")
    status = models.CharField("状态", max_length=16, default="OPEN")
    # OPEN / MATCHED / FILLED / CANCELLED / EXPIRED
    matched_order = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "NPC 交易订单"
        verbose_name_plural = "NPC 交易订单"
        constraints = [
            models.CheckConstraint(
                name="trade_order_amount_positive",
                condition=models.Q(amount__gt=0),
            ),
            models.CheckConstraint(
                name="trade_order_unit_price_positive",
                condition=models.Q(unit_price__gt=0),
            ),
        ]

    def __str__(self):
        return f"{self.order_id} {self.side} item={self.item_id} x{self.amount} @{self.unit_price}"


class NpcTradeDelivery(models.Model):
    """A delivery record linking a buyer and seller order."""

    delivery_id = models.UUIDField("交付ID", primary_key=True, default=uuid.uuid4)
    buyer_order = models.ForeignKey(
        NpcTradeOrder,
        on_delete=models.CASCADE,
        related_name="buyer_deliveries",
    )
    seller_order = models.ForeignKey(
        NpcTradeOrder,
        on_delete=models.CASCADE,
        related_name="seller_deliveries",
    )
    seller_slot_instance_id = models.UUIDField("卖方槽位批次ID")
    reserved_amount = models.IntegerField("预留数量")
    reserved_money = models.IntegerField("预留金额")
    delivery_point_code = models.CharField("交付点", max_length=64)
    expires_world_minute = models.BigIntegerField("过期世界分钟")
    status = models.CharField("状态", max_length=16, default="ACTIVE")
    # ACTIVE / COMPLETED / CANCELLED / EXPIRED
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "NPC 交易交付"
        verbose_name_plural = "NPC 交易交付"
        constraints = [
            models.CheckConstraint(
                name="delivery_reserved_amount_non_negative",
                condition=models.Q(reserved_amount__gte=0),
            ),
            models.CheckConstraint(
                name="delivery_reserved_money_non_negative",
                condition=models.Q(reserved_money__gte=0),
            ),
        ]

    def __str__(self):
        return f"{self.delivery_id} {self.status}"


class DailyPriceSnapshot(models.Model):
    """Daily price snapshot for village economy."""

    day_index = models.IntegerField("日索引", unique=True)
    prices = models.JSONField("价格", default=dict)
    # {item_id: price, ...}
    status = models.CharField("状态", max_length=16, default="DRAFT")
    # DRAFT / PUBLISHED
    published_by_npc_id = models.IntegerField("发布NPC ID", null=True, blank=True)
    published_world_minute = models.BigIntegerField("发布世界分钟", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "每日价格快照"
        verbose_name_plural = "每日价格快照"
        constraints = [
            models.CheckConstraint(
                name="daily_price_day_index_non_negative",
                condition=models.Q(day_index__gte=0),
            ),
        ]

    def __str__(self):
        return f"day={self.day_index} status={self.status}"


class VillageRestaurantState(models.Model):
    """Daily restaurant state for Lucy's diner."""

    day_index = models.IntegerField("日索引", unique=True)
    status = models.CharField("状态", max_length=16, default="OPEN")
    # OPEN / LOW_STOCK / SOLD_OUT
    traveler_sales_count = models.IntegerField("旅客销售数", default=0)
    traveler_revenue = models.IntegerField("旅客收入", default=0)
    asset_version = models.BigIntegerField("资产版本", default=0)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "餐厅状态"
        verbose_name_plural = "餐厅状态"
        constraints = [
            models.CheckConstraint(
                name="restaurant_traveler_sales_non_negative",
                condition=models.Q(traveler_sales_count__gte=0),
            ),
            models.CheckConstraint(
                name="restaurant_traveler_revenue_non_negative",
                condition=models.Q(traveler_revenue__gte=0),
            ),
        ]

    def __str__(self):
        return f"day={self.day_index} status={self.status}"


# ============================================================================
# Phase 3: Store & direct trade models
# ============================================================================

STORE_OPERATIONAL_STATUS_CHOICES = [
    (StoreOperationalStatus.OPEN.value, "营业中"),
    (StoreOperationalStatus.CLOSED.value, "打烊"),
    (StoreOperationalStatus.SUSPENDED.value, "停业"),
]

LISTING_STATUS_CHOICES = [
    (ListingStatus.ACTIVE.value, "在售"),
    (ListingStatus.SOLD_OUT.value, "售罄"),
    (ListingStatus.DELISTED.value, "已下架"),
]

PRICE_MODE_CHOICES = [
    (PriceMode.ORIGINAL.value, "原价"),
    (PriceMode.CUSTOM.value, "自定义"),
]

TRADE_OFFER_STATUS_CHOICES = [
    (TradeOfferStatus.PENDING.value, "待处理"),
    (TradeOfferStatus.ACCEPTED.value, "已接受"),
    (TradeOfferStatus.REJECTED.value, "已拒绝"),
    (TradeOfferStatus.CANCELLED.value, "已取消"),
    (TradeOfferStatus.EXPIRED.value, "已过期"),
]


class VillageStore(models.Model):
    """Independent store entity with its own inventory, listing and account."""

    store_code = models.CharField("商店代码", max_length=64, primary_key=True)
    location_code = models.CharField("地点码", max_length=64)
    operator_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operated_stores",
    )
    display_name = models.CharField("商店名称", max_length=100)
    status = models.CharField(
        "经营状态",
        max_length=16,
        choices=STORE_OPERATIONAL_STATUS_CHOICES,
        default=StoreOperationalStatus.OPEN.value,
    )
    transaction_fee_bps = models.IntegerField("交易费率(基点)", default=500)
    minimum_fee = models.IntegerField("最低费用", default=1)
    inventory_version = models.BigIntegerField("库存版本", default=0)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "村庄商店"
        verbose_name_plural = "村庄商店"
        ordering = ["store_code"]
        constraints = [
            models.CheckConstraint(
                name="village_store_fee_bps_in_range",
                condition=models.Q(transaction_fee_bps__gte=0, transaction_fee_bps__lte=10000),
            ),
            models.CheckConstraint(
                name="village_store_minimum_fee_non_negative",
                condition=models.Q(minimum_fee__gte=0),
            ),
        ]

    def __str__(self):
        return f"{self.store_code} - {self.display_name}"


class VillageStoreAccount(models.Model):
    """Store's independent money account (never the operator's personal wallet)."""

    store = models.OneToOneField(
        VillageStore,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="account",
    )
    balance = models.IntegerField("余额", default=0)
    account_version = models.BigIntegerField("账户版本", default=0)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "商店账户"
        verbose_name_plural = "商店账户"
        constraints = [
            models.CheckConstraint(
                name="village_store_account_balance_non_negative",
                condition=models.Q(balance__gte=0),
            ),
        ]

    def __str__(self):
        return f"{self.store.store_code} balance={self.balance}"


class VillageStoreInventorySlot(models.Model):
    """A store-owned item batch, preserving batch attributes on list/delist."""

    instance_id = models.UUIDField("批次ID", unique=True, default=uuid.uuid4)
    store = models.ForeignKey(
        VillageStore,
        on_delete=models.CASCADE,
        related_name="inventory_slots",
    )
    listing = models.ForeignKey(
        "VillageStoreListing",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_slots",
    )
    item_id = models.IntegerField("物品ID")
    item_amount = models.IntegerField("数量", default=0)
    produced_world_minute = models.BigIntegerField("生产世界分钟", null=True, blank=True)
    expires_world_minute = models.BigIntegerField("过期世界分钟", null=True, blank=True)
    source_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="store_inventory_slots",
    )
    source_slot_instance_id = models.UUIDField("来源批次ID", null=True, blank=True)
    reserved_amount = models.IntegerField("预留数量", default=0)

    class Meta:
        verbose_name = "商店库存批次"
        verbose_name_plural = "商店库存批次"
        constraints = [
            models.CheckConstraint(
                name="village_store_slot_amount_non_negative",
                condition=models.Q(item_amount__gte=0),
            ),
            models.CheckConstraint(
                name="village_store_slot_reserved_non_negative",
                condition=models.Q(reserved_amount__gte=0),
            ),
        ]

    def __str__(self):
        return f"{self.store.store_code} item={self.item_id} x{self.item_amount}"


class VillageStoreListing(models.Model):
    """A store's authoritative price listing for one item/price tier."""

    listing_id = models.UUIDField("上架ID", primary_key=True, default=uuid.uuid4)
    store = models.ForeignKey(
        VillageStore,
        on_delete=models.CASCADE,
        related_name="listings",
    )
    item_id = models.IntegerField("物品ID")
    price_mode = models.CharField(
        "价格模式",
        max_length=16,
        choices=PRICE_MODE_CHOICES,
        default=PriceMode.ORIGINAL.value,
    )
    unit_price = models.IntegerField("单价")
    status = models.CharField(
        "状态",
        max_length=16,
        choices=LISTING_STATUS_CHOICES,
        default=ListingStatus.ACTIVE.value,
    )
    listed_by_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.PROTECT,
        related_name="store_listings",
    )
    created_request_id = models.UUIDField("首次上架幂等键", unique=True)
    listed_world_minute = models.BigIntegerField("上架世界分钟")
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "商店上架记录"
        verbose_name_plural = "商店上架记录"
        constraints = [
            models.CheckConstraint(
                name="village_store_listing_unit_price_positive",
                condition=models.Q(unit_price__gt=0),
            ),
        ]

    def __str__(self):
        return f"{self.store.store_code} item={self.item_id} @{self.unit_price} {self.status}"


class VillageTradeOffer(models.Model):
    """Immutable directed NPC-to-NPC trade offer."""

    offer_id = models.UUIDField("报价ID", primary_key=True, default=uuid.uuid4)
    buyer_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="trade_offers_as_buyer",
    )
    seller_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="trade_offers_as_seller",
    )
    commitment = models.ForeignKey(
        "NpcCommitment",
        on_delete=models.SET_NULL,
        related_name="trade_offers",
        null=True,
        blank=True,
    )
    item_id = models.IntegerField("物品ID")
    amount = models.IntegerField("数量")
    gold_amount = models.IntegerField("金币总额")
    conversation_id = models.CharField("会话ID", max_length=128, null=True, blank=True)
    location_code = models.CharField("地点码", max_length=64)
    status = models.CharField(
        "状态",
        max_length=16,
        choices=TRADE_OFFER_STATUS_CHOICES,
        default=TradeOfferStatus.PENDING.value,
    )
    expected_seller_asset_version = models.BigIntegerField("期望卖方资产版本", null=True, blank=True)
    expires_world_minute = models.BigIntegerField("过期世界分钟")
    created_request_id = models.UUIDField("创建幂等键", unique=True)
    resolved_request_id = models.UUIDField("结束幂等键", null=True, blank=True)
    created_world_minute = models.BigIntegerField("创建世界分钟")
    resolved_world_minute = models.BigIntegerField("结束世界分钟", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "NPC 交易报价"
        verbose_name_plural = "NPC 交易报价"
        constraints = [
            models.CheckConstraint(
                name="village_trade_offer_amount_positive",
                condition=models.Q(amount__gt=0),
            ),
            models.CheckConstraint(
                name="village_trade_offer_gold_positive",
                condition=models.Q(gold_amount__gt=0),
            ),
            models.CheckConstraint(
                name="village_trade_offer_buyer_not_seller",
                condition=~models.Q(buyer_npc=models.F("seller_npc")),
            ),
            models.UniqueConstraint(
                fields=["resolved_request_id"],
                name="village_trade_offer_resolved_request_unique",
                condition=models.Q(resolved_request_id__isnull=False),
            ),
        ]

    def __str__(self):
        return f"{self.offer_id} {self.buyer_npc.agent_id}->{self.seller_npc.agent_id} item={self.item_id} {self.status}"


class NpcRelationship(models.Model):
    """Current directed relationship values between two formal NPCs."""

    from_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="relationships_from",
    )
    to_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="relationships_to",
    )
    affinity = models.SmallIntegerField(default=0)
    trust = models.SmallIntegerField(default=0)
    obligation = models.SmallIntegerField(default=0)
    conflict = models.PositiveSmallIntegerField(default=0)
    version = models.BigIntegerField(default=0)
    last_meaningful_event = models.ForeignKey(
        VillageWorldEvent,
        on_delete=models.SET_NULL,
        related_name="last_relationships",
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "NPC 关系"
        verbose_name_plural = "NPC 关系"
        ordering = ["from_npc__agent_id", "to_npc__agent_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["from_npc", "to_npc"],
                name="npc_relationship_pair_unique",
            ),
            models.CheckConstraint(
                condition=~models.Q(from_npc=models.F("to_npc")),
                name="npc_relationship_distinct_npcs",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(affinity__gte=-100, affinity__lte=100)
                    & models.Q(trust__gte=-100, trust__lte=100)
                    & models.Q(obligation__gte=-100, obligation__lte=100)
                    & models.Q(conflict__gte=0, conflict__lte=100)
                    & models.Q(version__gte=0)
                ),
                name="npc_relationship_values_in_range",
            ),
        ]

    def __str__(self):
        return f"{self.from_npc.agent_id}->{self.to_npc.agent_id}"


class NpcRelationshipChange(models.Model):
    """Idempotent audit record for one applied relationship change."""

    CATEGORY_CHOICES = [
        ("DELIVERY", "交货"),
        ("EMERGENCY_SUPPLY", "紧急供货"),
        ("BREACH", "违约"),
        ("JOINT_WORK", "共同工作"),
        ("RESCUE_GIFT", "救急赠予"),
    ]

    change_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    relationship = models.ForeignKey(
        NpcRelationship,
        on_delete=models.CASCADE,
        related_name="changes",
    )
    source_event = models.ForeignKey(
        VillageWorldEvent,
        on_delete=models.CASCADE,
        related_name="relationship_changes",
    )
    category = models.CharField(max_length=24, choices=CATEGORY_CHOICES)
    world_day = models.BigIntegerField()
    requested_delta = models.JSONField(default=dict)
    applied_delta = models.JSONField(default=dict)
    before_values = models.JSONField(default=dict)
    after_values = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "NPC 关系变更"
        verbose_name_plural = "NPC 关系变更"
        indexes = [
            models.Index(
                fields=["relationship", "world_day", "category"],
                name="npc_rel_change_day_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["relationship", "source_event"],
                name="npc_rel_change_event_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(world_day__gte=0),
                name="npc_rel_change_day_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.relationship} {self.category} day={self.world_day}"


class NpcCommitment(models.Model):
    """One shared model for all five structured NPC commitment types."""

    COMMITMENT_TYPE_CHOICES = [
        ("DELIVERY", "交货"),
        ("REPAYMENT", "还款"),
        ("DONATION", "捐赠"),
        ("JOINT_WORK", "共同工作"),
        ("MEETING", "见面"),
    ]
    STATUS_CHOICES = [
        ("OPEN", "开放"),
        ("COMPLETED", "已完成"),
        ("BREACHED", "已违约"),
        ("CANCELLED", "已取消"),
    ]

    commitment_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    commitment_type = models.CharField(max_length=16, choices=COMMITMENT_TYPE_CHOICES)
    promisor_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="commitments_as_promisor",
    )
    beneficiary_npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="commitments_as_beneficiary",
    )
    target = models.JSONField(default=dict)
    due_world_minute = models.BigIntegerField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="OPEN")
    source_request_id = models.UUIDField(unique=True)
    created_event = models.ForeignKey(
        VillageWorldEvent,
        on_delete=models.PROTECT,
        related_name="created_commitments",
    )
    settled_event = models.ForeignKey(
        VillageWorldEvent,
        on_delete=models.SET_NULL,
        related_name="settled_commitments",
        null=True,
        blank=True,
    )
    created_world_minute = models.BigIntegerField()
    settled_world_minute = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "NPC 承诺"
        verbose_name_plural = "NPC 承诺"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(promisor_npc=models.F("beneficiary_npc")),
                name="npc_commitment_distinct_npcs",
            ),
            models.CheckConstraint(
                condition=models.Q(due_world_minute__gte=0) & models.Q(created_world_minute__gte=0),
                name="npc_commitment_minutes_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.commitment_id} {self.commitment_type} {self.status}"


class NpcStoryFactAccess(models.Model):
    """Structured story facts currently unlocked for one NPC."""

    npc = models.ForeignKey(
        "inventory.NpcDefinition",
        on_delete=models.CASCADE,
        related_name="story_fact_access",
    )
    fact_code = models.CharField(max_length=64)
    unlocked_by_event = models.ForeignKey(
        VillageWorldEvent,
        on_delete=models.SET_NULL,
        related_name="npc_fact_unlocks",
        null=True,
        blank=True,
    )
    unlocked_world_minute = models.BigIntegerField()
    is_public = models.BooleanField(default=False)

    class Meta:
        verbose_name = "NPC 剧情事实权限"
        verbose_name_plural = "NPC 剧情事实权限"
        constraints = [
            models.UniqueConstraint(
                fields=["npc", "fact_code"],
                name="npc_story_fact_access_unique",
            ),
        ]
        ordering = ["npc__agent_id", "fact_code"]

    def __str__(self):
        return f"{self.npc.agent_id}:{self.fact_code}"


class PlayerStoryFactAccess(models.Model):
    """Private structured story facts unlocked for one authenticated player."""

    player_id = models.CharField(max_length=100)
    fact_code = models.CharField(max_length=64)
    unlocked_by_event = models.ForeignKey(
        VillageWorldEvent,
        on_delete=models.SET_NULL,
        related_name="player_fact_unlocks",
        null=True,
        blank=True,
    )
    unlocked_world_minute = models.BigIntegerField()

    class Meta:
        verbose_name = "玩家剧情事实权限"
        verbose_name_plural = "玩家剧情事实权限"
        constraints = [
            models.UniqueConstraint(
                fields=["player_id", "fact_code"],
                name="player_story_fact_access_unique",
            ),
        ]
        ordering = ["player_id", "fact_code"]

    def __str__(self):
        return f"{self.player_id}:{self.fact_code}"


# ============================================================================
# Phase 5: Broken Trade Route story state
# ============================================================================


class VillageStoryArc(models.Model):
    PHASE_CHOICES = [(value.value, value.value) for value in StoryPhase]
    PLAN_CHOICES = [(value.value, value.value) for value in StoryPlan]
    OUTCOME_CHOICES = [(value.value, value.value) for value in StoryOutcome]

    arc_code = models.CharField(max_length=64, primary_key=True)
    current_phase = models.CharField(max_length=32, choices=PHASE_CHOICES)
    current_plan = models.CharField(max_length=32, choices=PLAN_CHOICES, blank=True, default="")
    start_day = models.PositiveIntegerField(default=0)
    phase_entered_day = models.PositiveIntegerField(default=0)
    story_version = models.PositiveBigIntegerField(default=0)
    is_completed = models.BooleanField(default=False)
    outcome = models.CharField(max_length=32, choices=OUTCOME_CHOICES, blank=True, default="")
    last_crisis_day = models.IntegerField(null=True, blank=True)
    ration_until_day = models.IntegerField(null=True, blank=True)
    review_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class VillageStoryFact(models.Model):
    arc = models.OneToOneField(VillageStoryArc, on_delete=models.CASCADE, related_name="facts")
    bridge_condition = models.CharField(max_length=24, default="BROKEN")
    tool_stock_coverage_days = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    food_stock_coverage_days = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    repair_fund = models.PositiveIntegerField(default=0)
    repair_wood = models.PositiveIntegerField(default=0)
    repair_metal = models.PositiveIntegerField(default=0)
    repair_labor_hours = models.PositiveIntegerField(default=0)
    public_trust = models.SmallIntegerField(default=50)
    forest_route_known = models.BooleanField(default=False)
    forest_route_scouted = models.PositiveIntegerField(default=0)
    forest_route_supply = models.PositiveIntegerField(default=0)
    emergency_caravan_contacted = models.BooleanField(default=False)
    emergency_caravan_succeeded = models.BooleanField(default=False)
    bill_warning_revealed = models.BooleanField(default=False)
    mark_budget_delay_revealed = models.BooleanField(default=False)
    ration_rules_published = models.BooleanField(default=False)
    store_limits_enabled = models.BooleanField(default=False)
    ration_daily_limits = models.JSONField(default=dict, blank=True)
    community_meals = models.PositiveIntegerField(default=0)
    last_progress_day = models.IntegerField(default=0)
    stagnant_days = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class VillageStoryMeeting(models.Model):
    STATUS_CHOICES = [("OPEN", "开放"), ("SETTLED", "已结算"), ("FAILED", "已失败")]
    meeting_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    arc = models.ForeignKey(VillageStoryArc, on_delete=models.CASCADE, related_name="meetings")
    meeting_type = models.CharField(max_length=32, default="FIRST_COUNCIL")
    opened_day = models.PositiveIntegerField()
    closes_day = models.PositiveIntegerField()
    candidate_plans = models.JSONField(default=list)
    retry_count = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="OPEN")
    selected_plan = models.CharField(max_length=32, blank=True, default="")
    settled_day = models.IntegerField(null=True, blank=True)
    attendance_count = models.PositiveSmallIntegerField(default=0)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["arc", "meeting_type", "opened_day"],
            name="village_story_meeting_day_unique",
        )]


class VillageStoryVote(models.Model):
    VOTER_CHOICES = [("NPC", "NPC"), ("PLAYER", "PLAYER")]
    vote_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(VillageStoryMeeting, on_delete=models.CASCADE, related_name="votes")
    voter_type = models.CharField(max_length=16, choices=VOTER_CHOICES)
    voter_id = models.CharField(max_length=100)
    plan_code = models.CharField(max_length=32)
    player_support = models.BooleanField(default=False)
    request_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    submitted_world_minute = models.BigIntegerField()
    is_valid = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["meeting", "voter_type", "voter_id"],
            name="village_story_vote_latest_unique",
        )]


class VillageStoryTransitionReceipt(models.Model):
    request_id = models.UUIDField(primary_key=True, editable=False)
    arc = models.ForeignKey(VillageStoryArc, on_delete=models.CASCADE, related_name="transition_receipts")
    from_phase = models.CharField(max_length=32)
    to_phase = models.CharField(max_length=32)
    selected_plan = models.CharField(max_length=32, blank=True, default="")
    source_day = models.PositiveIntegerField()
    world_event = models.ForeignKey(VillageWorldEvent, on_delete=models.PROTECT, related_name="story_transitions")
    applied = models.BooleanField(default=True)
    response_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class VillageRouteAttempt(models.Model):
    route_attempt_id = models.UUIDField(primary_key=True, editable=False)
    arc = models.ForeignKey(VillageStoryArc, on_delete=models.CASCADE, related_name="route_attempts")
    request_id = models.UUIDField(unique=True)
    frozen_input = models.JSONField(default=dict)
    seed = models.CharField(max_length=128)
    result_code = models.CharField(max_length=32)
    cost = models.JSONField(default=dict, blank=True)
    injury = models.JSONField(default=dict, blank=True)
    progress = models.JSONField(default=dict, blank=True)
    created_world_minute = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
