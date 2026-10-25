from django.apps import AppConfig


class PlayerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "player"
    verbose_name = "玩家管理"

    def ready(self):
        from . import signals  # noqa: F401
