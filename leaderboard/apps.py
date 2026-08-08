import logging
import os
import sys
from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class LeaderboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "leaderboard"
    verbose_name = "排行榜与奖励"

    _scheduler_started = False

    def ready(self):
        if settings.DISABLE_BACKGROUND_SCHEDULERS:
            logger.info("[scheduler] Leaderboard scheduler disabled by configuration")
            return
        # Django 开发服务器会启动父、子两个进程；只在实际服务进程中注册任务。
        if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return
        if self.__class__._scheduler_started:
            return
        self.__class__._scheduler_started = True
        self._start_scheduler()

    def _start_scheduler(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            self._settle_periodic_leaderboards,
            trigger=CronTrigger(second=5),
            id="leaderboard_periodic_settlement",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=55,
        )
        scheduler.start()
        from config.scheduler_registry import register_scheduler
        register_scheduler("排行榜与奖励", scheduler)
        logger.info("[scheduler] Leaderboard periodic settlement scheduled every minute at second 5")

    @staticmethod
    def _settle_periodic_leaderboards():
        from .services import settle_due_periods

        try:
            settle_due_periods()
        except Exception:
            logger.exception("[scheduler] Periodic leaderboard settlement failed")
