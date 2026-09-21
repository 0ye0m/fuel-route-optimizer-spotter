"""
Django settings for the Fuel-Optimized Route Planning API project.

Every tunable business constant (vehicle range, fuel efficiency, corridor
width, external service URLs, data file locations) is driven by environment
variables (optionally via a local ``.env`` file) so the project can be
reconfigured without touching the code.
"""

from pathlib import Path
import os

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths & environment
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: str = "False") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: str) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


# ---------------------------------------------------------------------------
# Core Django configuration
# ---------------------------------------------------------------------------

SECRET_KEY = _env_str("SECRET_KEY", "django-insecure-dev-only-key-change-me")

DEBUG = _env_bool("DEBUG", "True")

ALLOWED_HOSTS = [h.strip() for h in _env_str("ALLOWED_HOSTS", "*").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "rest_framework",
    "routes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "fuel_route_optimizer.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "fuel_route_optimizer.wsgi.application"

# Targets the routes.tests package by default so `manage.py test` is robust
# in any environment (see fuel_route_optimizer/test_runner.py).
TEST_RUNNER = "fuel_route_optimizer.test_runner.RoutesTestRunner"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# DRF configuration
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "EXCEPTION_HANDLER": "routes.exception_handler.custom_exception_handler",
    "DEFAULT_THROTTLE_RATES": {},
}

# ---------------------------------------------------------------------------
# Vehicle & fuel-optimization business constants
# ---------------------------------------------------------------------------

# Maximum driving range on a full tank (miles).
MAX_RANGE_MILES = _env_float("MAX_RANGE_MILES", "500")
# Fuel efficiency (miles per gallon).
FUEL_EFFICIENCY_MPG = _env_float("FUEL_EFFICIENCY_MPG", "10")
# Corridor half-width around the route in which fuel stations are considered.
FUEL_STATION_SEARCH_RADIUS_MILES = _env_float("FUEL_STATION_SEARCH_RADIUS_MILES", "25")
# Resolution used to densify the route geometry before point-to-route math.
ROUTE_DENSIFY_STEP_MILES = _env_float("ROUTE_DENSIFY_STEP_MILES", "0.25")

# ---------------------------------------------------------------------------
# External services (free / no API key required)
# ---------------------------------------------------------------------------

NOMINATIM_BASE_URL = _env_str("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org")
NOMINATIM_USER_AGENT = _env_str("NOMINATIM_USER_AGENT", "fuel-route-optimizer/1.0")
# Resolve exact ``City, ST`` inputs from the bundled coordinate table before
# contacting Nominatim. More specific addresses still use Nominatim.
LOCAL_CITY_COORDINATES_ENABLED = _env_bool("LOCAL_CITY_COORDINATES_ENABLED", "True")
# Minimum spacing (seconds) between outbound Nominatim calls, per the
# Nominatim usage policy (max 1 request/second).
NOMINATIM_MIN_INTERVAL_SECONDS = _env_float("NOMINATIM_MIN_INTERVAL_SECONDS", "1.1")
OSRM_BASE_URL = _env_str("OSRM_BASE_URL", "https://router.project-osrm.org")
# Reuse deterministic OSRM responses for repeated coordinate pairs in a
# worker process; this avoids repeated public-network latency.
ROUTE_CACHE_ENABLED = _env_bool("ROUTE_CACHE_ENABLED", "True")
ROUTE_CACHE_MAX_ENTRIES = _env_int("ROUTE_CACHE_MAX_ENTRIES", "128")
EXTERNAL_API_TIMEOUT_SECONDS = _env_float("EXTERNAL_API_TIMEOUT_SECONDS", "15")

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------

FUEL_DATA_PATH = _env_str(
    "FUEL_DATA_PATH", str(BASE_DIR / "data" / "fuel-prices-for-be-assessment.csv")
)
CITY_COORDINATES_PATH = _env_str(
    "CITY_COORDINATES_PATH", str(BASE_DIR / "data" / "city_coordinates.json")
)

# ---------------------------------------------------------------------------
# Geo constants
# ---------------------------------------------------------------------------

# The fuel-price CSV contains US truck stops only; any row whose state is not
# in this set (e.g. Canadian provinces present in the raw file) is ignored.
US_STATE_CODES = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
        "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
        "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
        "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
        "WV", "WI", "WY",
    }
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "loggers": {
        "routes": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
