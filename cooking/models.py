from django.db import models


class PityConfig(models.Model):
    """保底机制配置（单例）。所有阈值和概率表均可通过后台修改。"""

    # 优秀 (quality >= 4)
    quality_4_normal_phase = models.IntegerField(default=15, help_text="优秀：概率不变的次数")
    quality_4_guarantee_at = models.IntegerField(default=20, help_text="优秀：必出次数")
    quality_4_ramp_probabilities = models.JSONField(
        default=list,
        help_text="优秀：概率提升阶段的概率数组（百分比），如 [10,25,50,80,100]",
    )
    quality_4_time_range = models.IntegerField(default=15, help_text="优秀保底触发时，烹饪时间区间（±N秒）")

    # 极品 (quality >= 5)
    quality_5_normal_phase = models.IntegerField(default=50, help_text="极品：概率不变的次数")
    quality_5_guarantee_at = models.IntegerField(default=60, help_text="极品：必出次数")
    quality_5_ramp_probabilities = models.JSONField(
        default=list,
        help_text="极品：概率提升阶段的概率数组（百分比）",
    )
    quality_5_time_range = models.IntegerField(default=5, help_text="极品保底触发时，烹饪时间区间（±N秒）")

    # 绝品 (quality >= 6)
    quality_6_normal_phase = models.IntegerField(default=75, help_text="绝品：概率不变的次数")
    quality_6_guarantee_at = models.IntegerField(default=90, help_text="绝品：必出次数")
    quality_6_ramp_probabilities = models.JSONField(
        default=list,
        help_text="绝品：概率提升阶段的概率数组（百分比）",
    )
    quality_6_time_range = models.IntegerField(default=0, help_text="绝品保底触发时，烹饪时间区间（±N秒，0=精确）")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "保底配置"
        verbose_name_plural = "保底配置"

    def __str__(self):
        return f"保底配置 (更新于 {self.updated_at:%Y-%m-%d %H:%M})"

    @classmethod
    def get_config(cls):
        """获取或创建默认配置（单例）。"""
        obj, _ = cls.objects.get_or_create(pk=1, defaults=_default_config())
        return obj


def _default_config():
    """首次创建时的默认配置值。"""
    return {
        "quality_4_normal_phase": 15,
        "quality_4_guarantee_at": 20,
        "quality_4_ramp_probabilities": [10, 25, 50, 80, 100],
        "quality_4_time_range": 15,
        "quality_5_normal_phase": 50,
        "quality_5_guarantee_at": 60,
        "quality_5_ramp_probabilities": [3, 6, 10, 16, 25, 35, 50, 65, 82, 100],
        "quality_5_time_range": 5,
        "quality_6_normal_phase": 75,
        "quality_6_guarantee_at": 90,
        "quality_6_ramp_probabilities": [2, 4, 6, 9, 13, 18, 24, 31, 39, 48, 58, 68, 79, 90, 100],
        "quality_6_time_range": 0,
    }


class PityCounter(models.Model):
    """每位玩家的保底计数器。"""

    player = models.OneToOneField(
        "account.GameAccount",
        on_delete=models.CASCADE,
        related_name="pity_counter",
        verbose_name="玩家",
    )
    count_4 = models.IntegerField(default=0, help_text="连续未出优秀的次数")
    count_5 = models.IntegerField(default=0, help_text="连续未出极品的次数")
    count_6 = models.IntegerField(default=0, help_text="连续未出绝品的次数")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "保底计数器"
        verbose_name_plural = "保底计数器"

    def __str__(self):
        return f"{self.player} 优秀:{self.count_4} 极品:{self.count_5} 绝品:{self.count_6}"

    @classmethod
    def get_or_create_for(cls, player):
        obj, _ = cls.objects.get_or_create(player=player)
        return obj
