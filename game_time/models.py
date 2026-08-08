from django.core.exceptions import ValidationError
from django.db import models


class GlobalGameTimeState(models.Model):
    """全服统一游戏时间状态（单例模型，由Env Server控制）。

    与 per-player 的 PlayerState 不同，此模型存储全服共享的游戏时间。
    Env Server 启动时加载，推进后回写。

    阶段一变更：
    - 新增 world_minute：从世界纪元 (0001-01-01 00:00) 起累计的 64 位绝对游戏分钟。
    - 新增 time_revision：每次成功时间提交后递增。
    - 年月日时分改为 world_minute 的校验投影，不再作为独立真相。
    """
    game_year = models.IntegerField("游戏年", default=2026)
    game_month = models.IntegerField("游戏月", default=3)
    game_day = models.IntegerField("游戏日", default=8, help_text="日历日（每月中的第几日）")
    game_hour = models.IntegerField("游戏小时", default=7)
    game_minute = models.IntegerField("游戏分钟", default=0)
    season = models.IntegerField("赛季序号", default=0)
    current_season_id = models.CharField("当前赛季标识", max_length=50, default="", blank=True)

    # 阶段一：权威时间字段
    world_minute = models.BigIntegerField(
        "世界分钟",
        default=0,
        help_text="从世界纪元 (0001-01-01 00:00) 起累计的绝对游戏分钟",
    )
    time_revision = models.BigIntegerField(
        "时间版本",
        default=0,
        help_text="每次成功时间提交后递增",
    )

    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "全局游戏时间"
        verbose_name_plural = "全局游戏时间"

    def __str__(self):
        return f"全局时间: {self.game_year}-{self.game_month}-{self.game_day} (wm={self.world_minute})"

    @classmethod
    def get_instance(cls):
        """获取或创建单例实例。"""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def clean(self):
        """校验 world_minute 与日历投影一致性。"""
        super().clean()
        if self.world_minute < 0:
            raise ValidationError({"world_minute": "world_minute 不能为负"})
        if self.time_revision < 0:
            raise ValidationError({"time_revision": "time_revision 不能为负"})

    @staticmethod
    def world_minute_from_calendar(year: int, month: int, day: int, hour: int, minute: int) -> int:
        """从日历计算 world_minute。

        世界纪元冻结为公历 0001-01-01 00:00：
        world_minute = (date.toordinal() - 1) * 1440 + hour * 60 + minute
        """
        from datetime import date
        d = date(year, month, day)
        return (d.toordinal() - 1) * 1440 + hour * 60 + minute

    def recalculate_world_minute(self):
        """从当前日历字段重新计算 world_minute。"""
        self.world_minute = self.world_minute_from_calendar(
            self.game_year, self.game_month, self.game_day,
            self.game_hour, self.game_minute
        )
