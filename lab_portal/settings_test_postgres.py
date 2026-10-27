"""PostgreSQL regression settings; use only a disposable database with CREATEDB."""
import os

from .settings_test import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("TEST_POSTGRES_DB", "portal_validation"),
        "USER": os.getenv("TEST_POSTGRES_USER", "postgres"),
        "PASSWORD": os.getenv("TEST_POSTGRES_PASSWORD", "postgres"),
        "HOST": os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.getenv("TEST_POSTGRES_PORT", "5432"),
    }
}
