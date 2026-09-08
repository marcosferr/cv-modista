"""Settings de cv-modista. App personal de un solo usuario: sin auth, SQLite, Redis local."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = _flag("DJANGO_DEBUG", "1")
ALLOWED_HOSTS = [h.strip() for h in os.getenv(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv("DJANGO_CSRF_ORIGINS", "").split(",")
                        if o.strip()]

# Detrás de nginx con TLS: sin esto Django cree que la conexión es HTTP y rompe el CSRF.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    X_FRAME_OPTIONS = "DENY"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django.contrib.messages",
    "apps.jobs",
    "apps.llm",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# SQLite en WAL: web y worker escriben en paralelo, WAL evita "database is locked".
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "job_create"
LOGOUT_REDIRECT_URL = "login"

# Registro abierto: cualquiera con el link se crea una cuenta. Como el cupo de
# OpenRouter es de la cuenta y no del usuario, un tope diario por usuario evita que
# uno solo se lleve todas las llamadas del día.
JOBS_PER_USER_PER_DAY = int(os.getenv("JOBS_PER_USER_PER_DAY", "15"))

LANGUAGE_CODE = "es"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# En producción los estáticos viven fuera del directorio de la app: nginx corre como
# www-data y necesita atravesar el path, y en el directorio de la app están el .env y
# la base SQLite. Sacarlos afuera evita tener que abrir ese directorio.
STATIC_ROOT = Path(os.getenv("DJANGO_STATIC_ROOT", BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "static"]
WHITENOISE_AUTOREFRESH = DEBUG

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# S3 en producción, disco en local. Los CVs y los PDFs generados son datos personales:
# el bucket va privado y todo se sirve con URLs prefirmadas de vida corta.
AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME", "")
AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "us-east-1")
USE_S3 = bool(AWS_STORAGE_BUCKET_NAME)

if USE_S3:
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": AWS_STORAGE_BUCKET_NAME,
                "region_name": AWS_S3_REGION_NAME,
                "default_acl": None,
                "querystring_auth": True,
                "querystring_expire": 900,
                "file_overwrite": False,
                "signature_version": "s3v4",
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }
else:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }

# Subida directa del navegador a S3 con URL prefirmada: el archivo no pasa por el
# servidor web, así que una subida grande no ocupa un worker de gunicorn.
PRESIGNED_UPLOAD_EXPIRE = 900
PRESIGNED_MAX_BYTES = 10 * 1024 * 1024

DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# --- Celery -----------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = 300
CELERY_TASK_TIME_LIMIT = 360
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_TRACK_STARTED = True
CELERY_RESULT_EXPIRES = 60 * 60 * 24
CELERY_TASK_ALWAYS_EAGER = _flag("CELERY_TASK_ALWAYS_EAGER")
CELERY_TASK_EAGER_PROPAGATES = True

# --- OpenRouter -------------------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Cupo de cuenta para modelos :free. 50/día con <USD 10 de créditos, 1000/día con 10 o más.
OPENROUTER_DAILY_QUOTA = int(os.getenv("OPENROUTER_DAILY_QUOTA", "50"))
OPENROUTER_TIMEOUT = 120.0
# Referer/Title opcionales: OpenRouter los usa para atribución en su ranking.
OPENROUTER_APP_URL = "http://localhost:8000"
OPENROUTER_APP_NAME = "cv-modista"

# --- proveedor de LLM -------------------------------------------------------
# "openrouter": modelos :free, gratis pero flojos y con cupo diario por cuenta.
# "bedrock": pago por token, con tool use forzado que garantiza JSON válido.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openrouter").lower()

BEDROCK_REGION = os.getenv("BEDROCK_REGION", AWS_S3_REGION_NAME)
# DeepSeek v4 Flash no existe en Bedrock: los DeepSeek disponibles son v3.2 (ON_DEMAND)
# y r1 (requiere perfil de inferencia). V3.2 sale ~USD 0,0044 por CV.
# Para bajar otro orden de magnitud: amazon.nova-lite-v1:0 (~USD 0,0005 por CV).
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "deepseek.v3.2")
BEDROCK_FALLBACK_MODEL_ID = os.getenv("BEDROCK_FALLBACK_MODEL_ID", "amazon.nova-lite-v1:0")
# Con registro abierto, un proveedor pago sin tope es una superficie de abuso.
BEDROCK_DAILY_USD_BUDGET = float(os.getenv("BEDROCK_DAILY_USD_BUDGET", "2.00"))

FAKE_LLM = _flag("FAKE_LLM")
FIXTURES_DIR = BASE_DIR / "fixtures" / "llm"

# --- LaTeX ------------------------------------------------------------------
TECTONIC_BIN = os.getenv("TECTONIC_BIN", "tectonic")
LATEX_TIMEOUT = 90

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.utils.autoreload": {"level": "WARNING"},
        "apps": {"level": "DEBUG" if DEBUG else "INFO"},
    },
}

# La lista de modelos :free rota seguido: se resincroniza a diario junto con el cupo.
CELERY_BEAT_SCHEDULE = {
    "sync-openrouter-models": {
        "task": "jobs.sync_models",
        "schedule": 6 * 3600,
    },
}
