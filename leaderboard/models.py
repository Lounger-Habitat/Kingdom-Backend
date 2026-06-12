from django.db import models


class BoardType(models.TextChoices):
    WEALTH = "wealth", "富豪榜"
    OUTPUT = "output", "产量榜"
    FRESHNESS = "freshness", "极鲜榜"
    TOTAL_WEALTH = "total_wealth", "总富豪榜"
    TOTAL_OUTPUT = "total_output", "总产量榜"
    TOTAL_FRESHNESS = "total_freshness", "总极鲜榜"


class SettlementType(models.TextChoices):
    DAILY = "daily", "每日结算"
    WEEKLY = "weekly", "每周结算"
    SEASON = "season", "赛季结算"


class Season(models.Model):
    """Represents a leaderboard season with a fixed real-world duration."""
    season_id = models.CharField(max_length=50, unique=True)
    sequence = models.IntegerField(unique=True, help_text="自增序号")
    duration_days = models.IntegerField(default=7, help_text="赛季时长（现实天数）")
    start_time = models.BigIntegerField(help_text="赛季开始时间（epoch seconds）")
    end_time = models.BigIntegerField(help_text="赛季结束时间（epoch seconds）")
    settled = models.BooleanField(default=False, help_text="是否已结算")
    status = models.CharField(max_length=20, default="active", choices=[
        ("active", "进行中"),
        ("ended", "已结束"),
        ("settled", "已结算"),
    ])

    class Meta:
        ordering = ["-sequence"]
        verbose_name = "赛季"
        verbose_name_plural = "赛季"

    def __str__(self):
        return f"{self.season_id} (#{self.sequence}) [{self.status}]"


class SeasonConfig(models.Model):
    """Singleton model for global season configuration."""
    default_duration_days = models.IntegerField(default=7, help_text="默认赛季天数")
    auto_rotate = models.BooleanField(default=True, help_text="是否自动轮转")
    freshness_top_n = models.IntegerField(default=3, help_text="极鲜榜每玩家最多上榜菜品数")

    class Meta:
        verbose_name = "赛季配置"
        verbose_name_plural = "赛季配置"

    @classmethod
    def get_instance(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"SeasonConfig(duration={self.default_duration_days}, auto={self.auto_rotate})"


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

    class Meta:
        ordering = ["board_type", "game_day", "rank"]

    def __str__(self):
        return f"[{self.board_type}] #{self.rank} {self.display_name}"


class RewardConfig(models.Model):
    board_type = models.CharField(max_length=20, choices=BoardType.choices)
    settlement_type = models.CharField(max_length=20, choices=SettlementType.choices)
    rank_min = models.IntegerField(default=1)
    rank_max = models.IntegerField(default=1)
    reward_type = models.CharField(max_length=20)
    reward_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["board_type", "settlement_type", "rank_min"]

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

    def __str__(self):
        return f"{self.player_id} - {self.event_type} @ {self.timestamp}"


class SeasonResetProxy(Season):
    """代理模型，用于在 Admin 侧边栏显示「赛季重置」入口。"""

    class Meta:
        proxy = True
        verbose_name = "赛季重置"
        verbose_name_plural = "赛季重置"


class SeasonTestProxy(Season):
    """代理模型，Admin 侧边栏「赛季轮转测试」入口。"""

    class Meta:
        proxy = True
        verbose_name = "赛季轮转测试"
        verbose_name_plural = "赛季轮转测试"
