from django.db import models
from account.models import GameAccount


class PlayerState(models.Model):
    player_id = models.CharField(max_length=100, unique=True)
    action_points = models.IntegerField(default=100)
    action_points_max = models.IntegerField(default=1000)
    # 当前游戏内日历（最近一次客户端上报）。注意 game_day 现在表示"日历的日"(day-of-month)。
    game_day = models.IntegerField(default=8)
    game_year = models.IntegerField(default=2026)
    game_month = models.IntegerField(default=3)
    game_hour = models.IntegerField(default=7)
    game_minute = models.IntegerField(default=0)
    season = models.IntegerField(default=0)
    # 累计游戏天数（"第 N 天"），由后端按日历日差累加得到，作为权威值。
    total_game_days = models.IntegerField(default=1)
    # 是否已用首次客户端上报作为基准；避免首报被算成跨数月的 delta。
    time_initialized = models.BooleanField(default=False)
    # 当前玩家所在的赛季 ID（空表示尚未迁移，等效于当前活跃赛季）
    current_season_id = models.CharField(max_length=50, default="", blank=True)

    class Meta:
        ordering = ["player_id"]

    def __str__(self):
        return f"{self.player_id} AP={self.action_points} day={self.game_day}"


class PlayerRecipe(models.Model):
    player = models.ForeignKey(PlayerState, on_delete=models.CASCADE, related_name="recipes")
    recipe_name = models.CharField(max_length=100)
    catalog_recipe = models.ForeignKey(
        "recipes.Recipe", on_delete=models.CASCADE, null=True, blank=True, related_name="player_recipes"
    )
    show_ingredient_count = models.BooleanField(default=False, help_text="是否在菜谱界面显示食材数量")

    class Meta:
        unique_together = [("player", "recipe_name")]

    def __str__(self):
        return f"{self.player.player_id} - {self.recipe_name}"


class DefaultRecipe(models.Model):
    """标记哪些菜谱是新玩家默认获得的（只存菜名，食材从 recipes.Recipe 目录查询）。"""
    recipe_name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["recipe_name"]

    def __str__(self):
        return self.recipe_name


class PlayerTitle(models.Model):
    player = models.ForeignKey(PlayerState, on_delete=models.CASCADE, related_name="titles")
    title_id = models.CharField(max_length=100)
    title_name = models.CharField(max_length=100)
    obtained_at = models.BigIntegerField(default=0)
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.player.player_id} - {self.title_name}"


class PlayerResetProxy(GameAccount):
    """代理模型，用于在 Admin 侧边栏显示'玩家重置'入口。"""

    class Meta:
        proxy = True
        verbose_name = "玩家重置"
        verbose_name_plural = "玩家重置"
