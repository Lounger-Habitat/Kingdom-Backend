from django.db import models


class GlobalGameTimeState(models.Model):
    """全服统一游戏时间状态（单例模型，由Env Server控制）。

    与 per-player 的 PlayerState 不同，此模型存储全服共享的游戏时间。
    Env Server 启动时加载，推进后回写。
    """
    game_year = models.IntegerField("游戏年", default=2026)
    game_month = models.IntegerField("游戏月", default=3)
    game_day = models.IntegerField("游戏日", default=8, help_text="日历日（每月中的第几日）")
    game_hour = models.IntegerField("游戏小时", default=7)
    game_minute = models.IntegerField("游戏分钟", default=0)
    season = models.IntegerField("赛季序号", default=0)
    current_season_id = models.CharField("当前赛季标识", max_length=50, default="", blank=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "全局游戏时间"
        verbose_name_plural = "全局游戏时间"

    def __str__(self):
        return f"全局时间: {self.game_year}-{self.game_month}-{self.game_day}"

    @classmethod
    def get_instance(cls):
        """获取或创建单例实例。"""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
