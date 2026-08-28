import os
import sys
from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "django-insecure-kingdom-dev-key-change-in-production"
)

DEBUG = os.environ.get("DJANGO_DEBUG", "True") == "True"

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "corsheaders",
    "import_export",
    "drf_spectacular",
    # Local apps
    "account.apps.AccountConfig",
    "items.apps.ItemsConfig",
    "recipes.apps.RecipesConfig",
    "inventory.apps.InventoryConfig",
    "player.apps.PlayerConfig",
    "season.apps.SeasonConfig",
    "leaderboard.apps.LeaderboardConfig",
    "task_scheduler.apps.TaskSchedulerConfig",
    "game_time.apps.GameTimeConfig",
    "cooking.apps.CookingConfig",
    "restaurant.apps.RestaurantConfig",
    "marketplace.apps.MarketplaceConfig",
    "autobuy.apps.AutobuyConfig",
    "village.apps.VillageConfig",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database: 环境变量 DB_ENGINE=postgresql 时使用 PostgreSQL，否则用 SQLite
if os.environ.get("DB_ENGINE") == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "kingdom"),
            "USER": os.environ.get("DB_USER", "kingdom"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "db"),
            "PORT": os.environ.get("DB_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {
                "timeout": 60,  # 秒，等待锁释放
                # 开发服务器是多线程的，Unity 并发写 checkpoint/accept/complete
                # 会抢 SQLite 文件锁导致间歇 500（database is locked）。
                # WAL：读写不互斥；IMMEDIATE：BEGIN 即取写锁，配合 busy 等待串行化写事务。
                "init_command": "PRAGMA journal_mode=WAL;",
                "transaction_mode": "IMMEDIATE",
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# CORS
_cors_origins = os.environ.get("CORS_ORIGINS", "")
if _cors_origins:
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins.split(",") if o.strip()]
else:
    CORS_ALLOW_ALL_ORIGINS = True

# DRF
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "account.authentication.GameJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "EXCEPTION_HANDLER": "rest_framework.views.exception_handler",
    "DEFAULT_PAGINATION_CLASS": None,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# JWT
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=2),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
}

# Swagger / OpenAPI
SPECTACULAR_SETTINGS = {
    "TITLE": "Kingdom Game API",
    "DESCRIPTION": "王国物语游戏后端接口文档",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# AI 点评服务
AI_SERVICE_URL = os.environ.get("AI_SERVICE_URL", "")
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_REQUEST_TIMEOUT = int(os.environ.get("AI_REQUEST_TIMEOUT", "30"))

# Tests and one-shot management commands must not start in-process schedulers.
# Production deployments should use an external scheduler and can set this flag
# while gradually moving away from the legacy AppConfig jobs.
DISABLE_BACKGROUND_SCHEDULERS = os.environ.get(
    "DISABLE_BACKGROUND_SCHEDULERS", "False"
).lower() in {"1", "true", "yes", "on"}

# Phase-zero safety gate. Formal village NPC assets remain read-only until the
# atomic economy slice is implemented in phase three.
VILLAGE_TEST_ASSET_ACTIONS_ENABLED = os.environ.get(
    "VILLAGE_TEST_ASSET_ACTIONS_ENABLED", "False"
).lower() in {"1", "true", "yes", "on"}

DEPLOYMENT_ENV = os.environ.get("DEPLOYMENT_ENV", "development").strip().lower()

# Development-only command path used for Hub -> Env Server -> Backend integration.
# Production always fails closed even if the environment variable is accidentally set.
VILLAGE_FAST_COMMAND_INTEGRATION = (
    DEPLOYMENT_ENV != "production"
    and os.environ.get(
        "VILLAGE_FAST_COMMAND_INTEGRATION",
        "False" if ("test" in sys.argv or "pytest" in Path(sys.argv[0]).name.lower()) else "False",
    ).lower()
    in {"1", "true", "yes", "on"}
)

# Logging: suppress noisy runserver access logs for high-frequency runtime
# tick endpoints, but only on success (2xx) — errors still print.
import logging
import re


class _SuppressRuntimeTickFilter(logging.Filter):
    _QUIET_PATHS = (
        "/game/time/sync",
        "/api/village/runtime/advance-needs",
        "/api/village/runtime/time",
        "/api/village/runtime/checkpoint",
        "/api/village/runtime/bootstrap",
        "/api/village/deliveries/pending",
    )
    # django access-log message: '"METHOD path HTTP/1.1" <code> <size>'
    _STATUS_RE = re.compile(r'"\s+(\d{3})\b')

    def filter(self, record):
        msg = record.getMessage()
        if not any(p in msg for p in self._QUIET_PATHS):
            return True
        m = self._STATUS_RE.search(msg)
        if m and 200 <= int(m.group(1)) < 300:
            return False  # success on a quiet path: suppress
        return True  # error (or unparseable): let it through


LOGGING = {
    "version": 1,
    "filters": {
        "suppress_runtime_tick": {
            "()": _SuppressRuntimeTickFilter,
        },
    },
    "loggers": {
        "django.server": {
            "level": "INFO",
            "filters": ["suppress_runtime_tick"],
            "propagate": True,
        },
    },
}
