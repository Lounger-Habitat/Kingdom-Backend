from django.db import models


class Season(models.Model):
    """Represents a leaderboard season with a fixed real-world duration."""
    season_id = models.CharField(max_length=50, unique=True)
    sequence = models.IntegerField(unique=True, help_text="自增序号")
    start_time = models.BigIntegerField(help_text="赛季开始时间（epoch seconds）")
    end_time = models.BigIntegerField(help_text="赛季结束时间（epoch seconds）")
    settled = models.BooleanField(default=False, help_text="是否已结算")
    status = models.CharField(max_length=20, default="active", choices=[
        ("active", "进行中"),
        ("ended", "已结束"),
        ("settled", "已结算"),
    ])

    class Meta:
        db_table = "leaderboard_season"
        ordering = ["-sequence"]
        verbose_name = "赛季"
        verbose_name_plural = "赛季"

    @property
    def duration_days(self):
        return SeasonConfig.get_instance().duration_days

    def __str__(self):
        return f"{self.season_id} (#{self.sequence}) [{self.status}]"


class SeasonConfig(models.Model):
    """Singleton model for global season configuration."""
    duration_days = models.IntegerField(default=7, help_text="赛季时长（现实天数）")
    max_game_days = models.IntegerField(default=0, help_text="全服最大可推进天数，0表示无限制")
    auto_rotate = models.BooleanField(default=True, help_text="是否自动轮转")
    freshness_top_n = models.IntegerField(default=3, help_text="极鲜榜每玩家最多上榜菜品数")
    milestone_days = models.IntegerField(default=10, help_text="总榜奖励结算间隔天数，0=禁用")

    class Meta:
        db_table = "leaderboard_seasonconfig"
        verbose_name = "赛季配置"
        verbose_name_plural = "赛季配置"

    @classmethod
    def get_instance(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"SeasonConfig(duration={self.duration_days}, auto={self.auto_rotate})"


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
