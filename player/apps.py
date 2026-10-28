from django.apps import AppConfig


class PlayerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "player"
    verbose_name = "玩家管理"

    def ready(self):
        from . import signals  # noqa: F401
        from .quick_input_scheduler import start_scheduler
        start_scheduler()
