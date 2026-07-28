from django.db import models


class GlobalGameTimeState(models.Model):
    """全服统一游戏时间状态（单例模型，由Env Server控制）。

    与 per-player 的 PlayerState 不同，此模型存储全服共享的游戏时间。
    Env Server 启动时加载，推进后回写。
    """
    game_year = models.IntegerField(default=2026)
    game_month = models.IntegerField(default=3)
    game_day = models.IntegerField(default=8, help_text="日历的日 (day-of-month)")
    game_hour = models.IntegerField(default=7)
    game_minute = models.IntegerField(default=0)
    season = models.IntegerField(default=0)
    total_game_days = models.IntegerField(default=1, help_text="累计游戏天数")
    current_season_id = models.CharField(max_length=50, default="", blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "全局游戏时间"
        verbose_name_plural = "全局游戏时间"

    def __str__(self):
        return f"全局时间: {self.game_year}-{self.game_month}-{self.game_day} 第{self.total_game_days}天"

    @classmethod
    def get_instance(cls):
        """获取或创建单例实例。"""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
