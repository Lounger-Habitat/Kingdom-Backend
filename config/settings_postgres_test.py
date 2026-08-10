"""Isolated PostgreSQL settings for phase-two concurrency tests.

Historical migrations are intentionally disabled here: the repository has a
pre-existing SQLite-only leaderboard migration, while these tests need the
current model state and real PostgreSQL row locks.
"""

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "kingdom_phase2_test",
        "USER": "postgres",
        "PASSWORD": "",
        "HOST": "127.0.0.1",
        "PORT": "55432",
        "TEST": {"NAME": "test_kingdom_phase2_test"},
    }
}

MIGRATION_MODULES = {
    "admin": None, "auth": None, "contenttypes": None, "sessions": None,
    "account": None, "autobuy": None, "cooking": None, "game_time": None,
    "inventory": None, "items": None, "leaderboard": None, "marketplace": None,
    "player": None, "recipes": None, "restaurant": None, "season": None,
    "task_scheduler": None, "village": None,
}
