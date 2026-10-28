import uuid

from django.db import models
from django.utils import timezone
from account.models import GameAccount


class PlayerState(models.Model):
    player_id = models.CharField(max_length=100, unique=True)
    action_points = models.IntegerField(default=200)
    action_points_max = models.IntegerField(default=1000)
    action_points_recovered_at = models.DateTimeField(default=timezone.now)
    # 当前游戏内日历（最近一次客户端上报）。注意 game_day 现在表示"日历的日"(day-of-month)。
    # 累计游戏天数（"第 N 天"），每次玩家点击下一日+1，赛季轮换时重置为1。
    # 是否已用首次客户端上报作为基准；避免首报被算成跨数月的 delta。
    # 当前玩家所在的赛季 ID（空表示尚未迁移，等效于当前活跃赛季）
    current_season_id = models.CharField(max_length=50, default="", blank=True)
    # 日初金币快照（用于富豪榜日榜计算：当前金币 - day_start_money）
    # 总榜金币快照（用于富豪榜总榜计算：snapshot + 当天增量）

    class Meta:
        ordering = ["player_id"]
        verbose_name = "玩家状态"
        verbose_name_plural = "玩家状态"

    def __str__(self):
        return f"{self.player_id} AP={self.action_points}"


class PlayerRecipe(models.Model):
    player = models.ForeignKey(PlayerState, on_delete=models.CASCADE, related_name="recipes")
    recipe_name = models.CharField(max_length=100)
    catalog_recipe = models.ForeignKey(
        "recipes.Recipe", on_delete=models.CASCADE, null=True, blank=True, related_name="player_recipes"
    )
    show_ingredient_count = models.BooleanField(default=False, help_text="是否在菜谱界面显示食材数量")

    class Meta:
        unique_together = [("player", "recipe_name")]
        verbose_name = "玩家菜谱"
        verbose_name_plural = "玩家菜谱"

    def __str__(self):
        return f"{self.player.player_id} - {self.recipe_name}"


class DefaultRecipe(models.Model):
    """标记哪些菜谱是新玩家默认获得的（只存菜名，食材从 recipes.Recipe 目录查询）。"""
    recipe_name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["recipe_name"]
        verbose_name = "新玩家默认菜谱"
        verbose_name_plural = "新玩家默认菜谱"

    def __str__(self):
        return self.recipe_name


class PlayerTitle(models.Model):
    player = models.ForeignKey(PlayerState, on_delete=models.CASCADE, related_name="titles")
    title_id = models.CharField(max_length=100)
    title_name = models.CharField(max_length=100)
    obtained_at = models.BigIntegerField(default=0)
    is_active = models.BooleanField(default=False)

    class Meta:
        verbose_name = "玩家称号"
        verbose_name_plural = "玩家称号"

    def __str__(self):
        return f"{self.player.player_id} - {self.title_name}"


class ActionPointCostConfig(models.Model):
    """行动力消耗配置，可在 Django Admin 中修改。"""
    cost_type = models.CharField(max_length=50, unique=True, help_text="消耗类型标识，如 collect / talk / cook / trade / judge")
    cost_amount = models.IntegerField(default=0, help_text="每次消耗的行动力数值")
    description = models.CharField(max_length=100, blank=True, default="", help_text="备注说明")

    class Meta:
        ordering = ["cost_type"]
        verbose_name = "行动力消耗配置"
        verbose_name_plural = "行动力消耗配置"

    def __str__(self):
        return f"{self.cost_type} = {self.cost_amount}"

    @classmethod
    def get_cost(cls, cost_type: str) -> int:
        """根据类型获取消耗量，未配置的类型返回 0。"""
        try:
            return cls.objects.get(cost_type=cost_type).cost_amount
        except cls.DoesNotExist:
            return 0


class PlayerResetProxy(GameAccount):
    """代理模型，用于在 Admin 侧边栏显示'玩家重置'入口。"""

    class Meta:
        proxy = True
        verbose_name = "玩家重置"
        verbose_name_plural = "玩家重置"


class PlayerGrowthState(models.Model):
    player = models.OneToOneField(GameAccount, on_delete=models.CASCADE, related_name="growth_state")
    revision = models.PositiveBigIntegerField(default=0)
    legacy_migration_version = models.PositiveIntegerField(default=0)
    legacy_entitlements = models.JSONField(default=dict, blank=True)
    tracked_goal_code = models.CharField(max_length=50, default="", blank=True)

    class Meta:
        verbose_name = "玩家成长档案"
        verbose_name_plural = "玩家成长档案"


class PlayerTutorialState(models.Model):
    """Account-owned teaching progress; never a source of money or skill XP."""
    player = models.OneToOneField(GameAccount, on_delete=models.CASCADE, related_name="tutorial_state")
    version = models.PositiveIntegerField(default=1)
    revision = models.PositiveBigIntegerField(default=0)
    started = models.BooleanField(default=False)
    paused = models.BooleanField(default=False)
    route_code = models.CharField(max_length=20, default="basics")
    evidence = models.JSONField(default=dict, blank=True)
    completed = models.JSONField(default=dict, blank=True)
    lucy_context_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    lucy_commission = models.OneToOneField("village.VillagePersonalCommission", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="lucy_tutorial")
    lucy_conversation_id = models.CharField(max_length=256, blank=True, default="")
    lucy_thanks_conversation_id = models.CharField(max_length=256, blank=True, default="")
    lucy_thanks_speech_id = models.CharField(max_length=256, blank=True, default="")

    class Meta:
        verbose_name = "玩家教学进度"
        verbose_name_plural = "玩家教学进度"


class PlayerArrivalState(models.Model):
    """One-off personal arrival, independent of teaching and shared bed leases."""
    player = models.OneToOneField(GameAccount, on_delete=models.CASCADE, related_name="arrival_state")
    eligible = models.BooleanField(default=False)
    revision = models.PositiveBigIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    finish_reason = models.CharField(max_length=20, default="", blank=True)

    class Meta:
        verbose_name = "玩家入村开场"
        verbose_name_plural = "玩家入村开场"


class PlayerSkillProgress(models.Model):
    from .progression_rules import SKILLS

    player = models.ForeignKey(GameAccount, on_delete=models.CASCADE, related_name="skill_progress")
    skill_code = models.CharField(max_length=20, choices=SKILLS)
    total_xp = models.PositiveBigIntegerField(default=0)
    rules_version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["player", "skill_code"], name="player_skill_unique")]
        verbose_name = "玩家技能进度"
        verbose_name_plural = "玩家技能进度"

    @property
    def level(self):
        from .progression_rules import level_for_xp
        return level_for_xp(self.total_xp)


class SkillRewardReceipt(models.Model):
    player = models.ForeignKey(GameAccount, on_delete=models.CASCADE, related_name="skill_reward_receipts")
    source_type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=128)
    skill_code = models.CharField(max_length=20, choices=PlayerSkillProgress.SKILLS)
    rules_version = models.PositiveIntegerField(default=1)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["player", "source_type", "source_id", "skill_code"],
                                               name="player_skill_reward_source_unique")]
        verbose_name = "技能奖励回执"
        verbose_name_plural = "技能奖励回执"


class PlayerMilestoneProgress(models.Model):
    player = models.ForeignKey(GameAccount, on_delete=models.CASCADE, related_name="growth_milestones")
    code = models.CharField(max_length=50)
    title = models.CharField(max_length=100)
    source_type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=128)
    rules_version = models.PositiveIntegerField(default=1)
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["player", "code"], name="player_milestone_unique")]
        verbose_name = "玩家成长里程碑"
        verbose_name_plural = "玩家成长里程碑"


class ItemGameplayDefinition(models.Model):
    """Static capabilities only. G0 does not enforce new equipment gates."""
    item = models.OneToOneField("items.Item", on_delete=models.CASCADE, related_name="gameplay_definition")
    capability_code = models.CharField(max_length=40)
    tier = models.PositiveSmallIntegerField(default=1, choices=[(1, "基础"), (2, "改良"), (3, "专业")])
    required_skill = models.CharField(max_length=20, choices=PlayerSkillProgress.SKILLS, blank=True, default="")
    required_level = models.PositiveSmallIntegerField(default=1)
    effects = models.JSONField(default=list, blank=True)
    rules_version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(tier__gte=1, tier__lte=3), name="item_gameplay_valid_tier"),
            models.CheckConstraint(condition=models.Q(required_level__gte=1, required_level__lte=10),
                                   name="item_gameplay_valid_level"),
        ]
        verbose_name = "物品玩法能力定义"
        verbose_name_plural = "物品玩法能力定义"


class PlayerHuntingState(models.Model):
    player = models.OneToOneField(GameAccount, on_delete=models.CASCADE, related_name="hunting_state")
    current_health = models.PositiveSmallIntegerField(default=100)
    revision = models.PositiveBigIntegerField(default=0)
    recovery_ready_world_minute = models.BigIntegerField(null=True, blank=True)
    authority_session_id = models.CharField(max_length=128, default="", blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(current_health__lte=100), name="player_health_maximum")]


class PlayerCombatReceipt(models.Model):
    player = models.ForeignKey(GameAccount, on_delete=models.PROTECT, related_name="combat_receipts")
    request_id = models.UUIDField()
    authority_session_id = models.CharField(max_length=128)
    payload_hash = models.CharField(max_length=64)
    action = models.CharField(max_length=32)
    response = models.JSONField(default=dict)
    world_minute = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["player", "request_id"], name="player_combat_request_unique")]


class PlayerMasteryChallenge(models.Model):
    player = models.ForeignKey(GameAccount, on_delete=models.CASCADE, related_name="mastery_challenges")
    skill_code = models.CharField(max_length=20, choices=PlayerSkillProgress.SKILLS)
    started_world_minute = models.BigIntegerField()
    rule_version = models.PositiveIntegerField(default=1)
    rules_snapshot = models.JSONField(default=dict)
    contributed_world_minute = models.BigIntegerField(null=True, blank=True)
    contribution_snapshot = models.JSONField(default=list)
    claimed_world_minute = models.BigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["player", "skill_code"], name="player_mastery_skill_unique")]


from .equipment_models import EquipmentDefinition, EquipmentLevel, WeaponMove  # noqa: E402,F401
