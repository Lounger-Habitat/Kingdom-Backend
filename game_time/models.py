from django.db import models


class GameTimeState(models.Model):
    """Singleton model tracking global game time state."""
    game_day = models.IntegerField(default=1)
    game_year = models.IntegerField(default=2026)
    game_month = models.IntegerField(default=1)
    game_hour = models.IntegerField(default=7)
    game_minute = models.IntegerField(default=0)
    season = models.IntegerField(default=0)
    max_game_days = models.IntegerField(default=0, help_text="全服最大可推进天数，0表示无限制")

    class Meta:
        verbose_name = "Game Time State"
        verbose_name_plural = "Game Time State"

    def __str__(self):
        return f"Day {self.game_day}, Year {self.game_year}, Month {self.game_month}, Season {self.season}"

    @classmethod
    def get_instance(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
