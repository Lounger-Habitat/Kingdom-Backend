from django.db import models


class BoardType(models.TextChoices):
    TOTAL_WEALTH = "total_wealth", "总富豪榜"
    TOTAL_OUTPUT = "total_output", "总产量榜"
    TOTAL_FRESHNESS = "total_freshness", "总极鲜榜"


class SettlementType(models.TextChoices):
    PERIODIC = "periodic", "周期结算"
    SEASON = "season", "赛季结算"
    MILESTONE = "milestone", "里程碑结算"


class LeaderboardEntry(models.Model):
    board_type = models.CharField(max_length=20, choices=BoardType.choices)
    season_id = models.CharField(max_length=50)
    character_id = models.CharField(max_length=100)
    display_name = models.CharField(max_length=100)
    is_ai = models.BooleanField(default=False)
    score = models.FloatField(default=0)
    rank = models.IntegerField(default=0)
    title = models.CharField(max_length=100, blank=True, default="")
    avatar_id = models.CharField(max_length=100, blank=True, default="")
    dish_name = models.CharField(max_length=100, blank=True, default="")
    game_day = models.IntegerField(null=True, blank=True)
    period_key = models.CharField(max_length=160, default="", blank=True, db_index=True)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["board_type", "game_day", "rank"]
        verbose_name = "排行榜条目"
        verbose_name_plural = "排行榜条目"

    def __str__(self):
        return f"[{self.board_type}] #{self.rank} {self.display_name}"


class RewardConfig(models.Model):
    board_type = models.CharField(max_length=20, choices=BoardType.choices)
    settlement_type = models.CharField(max_length=20, choices=SettlementType.choices, default=SettlementType.PERIODIC)
    period_config = models.ForeignKey("LeaderboardPeriodConfig", null=True, blank=True, on_delete=models.CASCADE, related_name="reward_configs")
    rank_min = models.IntegerField(default=1)
    rank_max = models.IntegerField(default=1)
    reward_type = models.CharField(max_length=20)
    reward_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["board_type", "settlement_type", "rank_min"]
        verbose_name = "排行榜奖励配置"
        verbose_name_plural = "排行榜奖励配置"

    def __str__(self):
        return f"[{self.board_type}] {self.settlement_type} #{self.rank_min}-{self.rank_max} -> {self.reward_type}"


class PendingReward(models.Model):
    player_id = models.CharField(max_length=100)
    reward_id = models.CharField(max_length=100, unique=True)
    board_type = models.CharField(max_length=20)
    settlement_type = models.CharField(max_length=20)
    rank = models.IntegerField(default=0)
    rewards = models.JSONField(default=list)
    expire_time = models.BigIntegerField(default=0)
    claimed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-expire_time"]
        verbose_name = "待领取排行榜奖励"
        verbose_name_plural = "待领取排行榜奖励"

    def __str__(self):
        return f"{self.player_id} - {self.reward_id} (claimed={self.claimed})"


class ReportEvent(models.Model):
    player_id = models.CharField(max_length=100)
    event_type = models.CharField(max_length=50)
    payload = models.JSONField(default=dict, blank=True)
    timestamp = models.BigIntegerField(default=0)
    game_day = models.IntegerField(null=True, blank=True, db_index=True)
    season_id = models.CharField(max_length=50, default="", blank=True, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "排行榜上报事件"
        verbose_name_plural = "排行榜上报事件"

    def __str__(self):
        return f"{self.player_id} - {self.event_type} @ {self.timestamp}"


class LeaderboardPeriodConfig(models.Model):
    """按北京时间自然零点切分的增量榜周期。interval_hours 必须整除 24。"""
    name = models.CharField("配置名称", max_length=100, unique=True)
    interval_hours = models.PositiveSmallIntegerField("结算周期（小时）", default=24)
    enabled = models.BooleanField("启用", default=True)
    reward_expire_hours = models.PositiveIntegerField("奖励过期时间（小时）", default=168)

    class Meta:
        ordering = ["interval_hours", "name"]
        verbose_name = "排行榜周期配置"
        verbose_name_plural = "排行榜周期配置"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.interval_hours not in (1, 2, 3, 4, 6, 8, 12, 24):
            raise ValidationError({"interval_hours": "必须是可整除 24 的小时数。"})

    def __str__(self):
        return f"{self.name} / {self.interval_hours}h"
