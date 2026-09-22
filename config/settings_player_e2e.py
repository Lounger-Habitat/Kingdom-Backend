"""Explicitly isolated local integration fixture; never use for production."""
from .settings import *  # noqa: F401,F403

E2E_DIRECTORY = BASE_DIR.parent / "Temp"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
    "NAME": E2E_DIRECTORY / "village-player-e2e.sqlite3",
    "OPTIONS": {"timeout": 60, "transaction_mode": "IMMEDIATE", "init_command": "PRAGMA journal_mode=WAL;"}}}
DISABLE_BACKGROUND_SCHEDULERS = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
# Fixture tokens cannot authenticate against the real development service.
SECRET_KEY = (E2E_DIRECTORY / "village-player-e2e.key").read_text(encoding="utf-8").strip()
SIMPLE_JWT = {**SIMPLE_JWT, "SIGNING_KEY": SECRET_KEY}
