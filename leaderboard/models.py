from django.db import models


class BoardType(models.TextChoices):
    WEALTH = "wealth", "富豪榜"
    OUTPUT = "output", "产量榜"
    FRESHNESS = "freshness", "极鲜榜"


class SettlementType(models.TextChoices):
    DAILY = "daily", "每日结算"
    WEEKLY = "weekly", "每周结算"


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

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.player_id} - {self.event_type} @ {self.timestamp}"
