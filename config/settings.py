"""
Django settings for the Smart Inventory Intelligence System.

Configuration is read from environment variables (optionally from a local `.env`
file). See `.env.example` for every supported variable.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) so no extra dependency is needed."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    return str(env(key, default)).lower() in {"1", "true", "yes", "on"}


def env_list(key, default=""):
    return [item.strip() for item in str(env(key, default)).split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Third party
    "rest_framework",
    "rest_framework.authtoken",
    # Project apps
    "apps.accounts",
    "apps.inventory",
    "apps.sales",
    "apps.purchasing",
    "apps.intelligence",
    "apps.iot",
    "apps.dashboard",
    "apps.api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.accounts.middleware.CurrentBusinessMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context_processors.business_context",
            ],
            "builtins": ["django.contrib.humanize.templatetags.humanize", "apps.dashboard.templatetags.sis"],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------- #
# Database: PostgreSQL when POSTGRES_DB is set, SQLite otherwise (development)
# --------------------------------------------------------------------------- #
if env("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB"),
            "USER": env("POSTGRES_USER", "postgres"),
            "PASSWORD": env("POSTGRES_PASSWORD", ""),
            "HOST": env("POSTGRES_HOST", "localhost"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "ATOMIC_REQUESTS": False,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "accounts:login"

# --------------------------------------------------------------------------- #
# Internationalisation (Kigali, Rwanda)
# --------------------------------------------------------------------------- #
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", "Africa/Kigali")
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------- #
# Static files
# --------------------------------------------------------------------------- #
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# --------------------------------------------------------------------------- #
# Django REST Framework
# --------------------------------------------------------------------------- #
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "2000/hour", "iot_device": "720/hour"},
}

# --------------------------------------------------------------------------- #
# Inventory intelligence defaults (each business can override in its settings)
# --------------------------------------------------------------------------- #
INTELLIGENCE = {
    "FORECAST_HORIZON_DAYS": int(env("SIS_FORECAST_HORIZON_DAYS", 14)),
    "HOLDOUT_DAYS": int(env("SIS_HOLDOUT_DAYS", 28)),
    "MIN_HISTORY_DAYS": int(env("SIS_MIN_HISTORY_DAYS", 42)),
    "HISTORY_WINDOW_DAYS": int(env("SIS_HISTORY_WINDOW_DAYS", 365)),
    "OVERSTOCK_COVER_DAYS": int(env("SIS_OVERSTOCK_COVER_DAYS", 60)),
    "EXPIRY_WARNING_DAYS": int(env("SIS_EXPIRY_WARNING_DAYS", 30)),
    "SERVICE_LEVEL_Z": float(env("SIS_SERVICE_LEVEL_Z", 1.65)),  # ~95% cycle service level
}

# --------------------------------------------------------------------------- #
# IoT / MQTT
# --------------------------------------------------------------------------- #
MQTT = {
    "HOST": env("MQTT_HOST", "localhost"),
    "PORT": int(env("MQTT_PORT", 1883)),
    "USERNAME": env("MQTT_USERNAME", ""),
    "PASSWORD": env("MQTT_PASSWORD", ""),
    "TLS": env_bool("MQTT_TLS", False),
    "TOPIC": env("MQTT_TOPIC", "sis/+/readings"),
}

# --------------------------------------------------------------------------- #
# Security hardening for production (DJANGO_DEBUG=False)
# --------------------------------------------------------------------------- #
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", 3600))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}
