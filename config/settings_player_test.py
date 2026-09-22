"""Isolated in-memory SQLite tests; never opens the saved village database."""
from .settings import *  # noqa: F401,F403

DISABLE_BACKGROUND_SCHEDULERS = True

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
                          "TEST": {"NAME": ":memory:"}}}
