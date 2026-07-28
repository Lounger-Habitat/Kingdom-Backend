import os
import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class AutobuyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "autobuy"
    verbose_name = "自动购买"

    def ready(self):
        if os.environ.get("RUN_MAIN") == "true" or not os.environ.get("RUN_MAIN"):
            self._start_scheduler()

    def _start_scheduler(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            self._run_auto_buy,
            trigger=IntervalTrigger(minutes=5),
            id="auto_buy",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        scheduler.start()
        logger.info("[scheduler] Auto-buy scheduled every 5 minutes (interval configurable in admin, requires restart)")

    @staticmethod
    def _run_auto_buy():
        from .services import run_auto_buy
        try:
            run_auto_buy()
        except Exception:
            logger.exception("[auto_buy] Unhandled error in run_auto_buy")
