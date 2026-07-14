import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class LeaderboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "leaderboard"
    verbose_name = "排行榜"
