"""Disposable appearance integration database; no saved village data or schedulers."""
from .settings_player_test import *  # noqa: F401,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
    "NAME": BASE_DIR.parent / "Temp" / "appearance-e2e.sqlite3", "OPTIONS": {"timeout": 30}}}
SECRET_KEY = "local-disposable-appearance-test-only-not-a-production-secret"
SIMPLE_JWT = {**SIMPLE_JWT, "SIGNING_KEY": SECRET_KEY}
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
