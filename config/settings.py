import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "routing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",  # route geometry compresses ~5x
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

# SQLite out of the box; set POSTGRES_DB (+ POSTGRES_USER/PASSWORD/HOST/PORT) to use PostgreSQL.
if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

# In-process cache by default; set REDIS_URL to share the route cache across workers.
if os.environ.get("REDIS_URL"):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": os.environ["REDIS_URL"]}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "OPTIONS": {"MAX_ENTRIES": 2000}}}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer", "rest_framework.renderers.BrowsableAPIRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser", "rest_framework.parsers.FormParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    # Each uncached plan can hit the free OSRM / Nominatim services; cap a single client.
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": os.environ.get("API_RATE_LIMIT", "60/min")},
    # Throttle on the socket address; behind N trusted proxies set NUM_PROXIES=N,
    # otherwise a client could dodge the limit with a forged X-Forwarded-For.
    "NUM_PROXIES": int(os.environ.get("NUM_PROXIES", 0)),
    "UNAUTHENTICATED_USER": None,
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Trip planning -----------------------------------------------------------
VEHICLE_MAX_RANGE_MILES = 500
VEHICLE_MPG = 10
DEFAULT_CORRIDOR_MILES = float(os.environ.get("DEFAULT_CORRIDOR_MILES", 10))
# The optimizer minimises pump cost + STOP_COST per stop. 0 (default) is the pure
# cheapest-fuel plan the assignment asks for; a few dollars stands in for the time and
# detour of a stop (NYC->LA: $5 cuts 17 stops to 6 for ~$7 more fuel).
DEFAULT_STOP_COST = float(os.environ.get("DEFAULT_STOP_COST", 0))
DEFAULT_RESERVE_MILES = float(os.environ.get("DEFAULT_RESERVE_MILES", 0))

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
HTTP_USER_AGENT = "spotter-fuel-route/1.0 (assessment)"  # same as routing.nominatim.USER_AGENT
EXTERNAL_TIMEOUT_SECONDS = 10  # Nominatim
OSRM_TIMEOUT = (4, 10)  # (connect, read) seconds; a coast-to-coast geometry is ~1 MB
OSRM_RETRIES = 1
OSRM_RETRY_BACKOFF_SECONDS = 0.5
ROUTE_CACHE_SECONDS = 60 * 60 * 24
