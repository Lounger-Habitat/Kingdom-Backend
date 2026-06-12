import os
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class LeaderboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "leaderboard"
    verbose_name = "排行榜"

    def ready(self):
        # Django dev server reloader 会启动两个进程，只在子进程启动调度器
        if os.environ.get("RUN_MAIN") == "true" or not os.environ.get("RUN_MAIN"):
            self._start_scheduler()

    def _start_scheduler(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            self._rotate_season,
            trigger=CronTrigger(hour=0, minute=5),  # 每天 00:05 创建新赛季
            id="rotate_season",
            replace_existing=True,
        )
        scheduler.add_job(
            self._settle_old_season,
            trigger=CronTrigger(hour=4, minute=0),  # 每天 04:00 结算旧赛季
            id="settle_old_season",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("[scheduler] Season tasks scheduled: rotate at 00:05, settle at 04:00")

    @staticmethod
    def _rotate_season():
        from .season_service import rotate_season
        season = rotate_season()
        logger.info("[scheduler] rotate_season done, active: %s", season.season_id)

    @staticmethod
    def _settle_old_season():
        from .season_service import settle_and_transition_old_season
        settle_and_transition_old_season()
        logger.info("[scheduler] settle_and_transition_old_season done")
