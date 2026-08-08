import os
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class SeasonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "season"
    verbose_name = "赛季管理"

    def ready(self):
        if os.environ.get("RUN_MAIN") == "true" or not os.environ.get("RUN_MAIN"):
            self._start_scheduler()

    def _start_scheduler(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            self._rotate_season,
            trigger=CronTrigger(hour=0, minute=5),
            id="rotate_season",
            replace_existing=True,
        )
        scheduler.add_job(
            self._settle_old_season,
            trigger=CronTrigger(hour=4, minute=0),
            id="settle_old_season",
            replace_existing=True,
        )
        scheduler.start()
        from config.scheduler_registry import register_scheduler
        register_scheduler("赛季管理", scheduler)
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
