"""
Pytest configuration for Django-Bolt tests.

Configures Django once for the test process. Each test process gets a new
SQLite database in a temporary directory, removed at exit, so no schema from
an earlier run survives.
"""

import logging
import os
import shutil
import sys
import sysconfig
import tempfile

import pytest

# Suppress httpx INFO logs during tests
logging.getLogger("httpx").setLevel(logging.WARNING)


@pytest.fixture(scope="session", autouse=True)
def _gil_stays_disabled():
    """Fail a free-threaded run when an import turns the GIL back on.

    CPython enables the GIL when it loads a C extension that does not declare
    free-threading support. The run then tests GIL behavior, not free threading.
    """
    yield
    if sysconfig.get_config_var("Py_GIL_DISABLED") and sys._is_gil_enabled():
        pytest.fail("An imported C extension enabled the GIL. The RuntimeWarning in the warnings summary names it.")


def pytest_configure(config):
    """Configure Django settings for pytest-django."""
    # One directory per test process (each xdist worker has its own).
    config._bolt_db_dir = tempfile.mkdtemp(prefix="django_bolt_test_")
    import django  # noqa: PLC0415
    from django.conf import settings  # noqa: PLC0415

    # Skip configuration if DJANGO_SETTINGS_MODULE is already set
    # This allows specific test modules to use their own Django settings
    if os.getenv("DJANGO_SETTINGS_MODULE"):
        return

    if not settings.configured:
        # Configure with all apps including admin to support all tests
        # The admin apps don't significantly impact non-admin tests
        settings.configure(
            DEBUG=True,
            SECRET_KEY="test-secret-key-global",
            ALLOWED_HOSTS=["*"],
            INSTALLED_APPS=[
                "django.contrib.admin",
                "django.contrib.auth",
                "django.contrib.contenttypes",
                "django.contrib.sessions",
                "django.contrib.messages",
                "django.contrib.staticfiles",
                "django_bolt",
            ],
            MIDDLEWARE=[
                "django.middleware.security.SecurityMiddleware",
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.common.CommonMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "django.contrib.messages.middleware.MessageMiddleware",
                "django.middleware.clickjacking.XFrameOptionsMiddleware",
            ],
            ROOT_URLCONF="tests.admin_tests.urls",
            TEMPLATES=[
                {
                    "BACKEND": "django.template.backends.django.DjangoTemplates",
                    "DIRS": [],
                    "OPTIONS": {
                        "context_processors": [
                            "django.template.context_processors.debug",
                            "django.template.context_processors.request",
                            "django.contrib.auth.context_processors.auth",
                            "django.contrib.messages.context_processors.messages",
                        ],
                        "loaders": [
                            "django.template.loaders.app_directories.Loader",
                            (
                                "django.template.loaders.locmem.Loader",
                                {
                                    "test_dashboard.html": "<html><body><h1>{{ title }}</h1></body></html>",
                                    "test_static_page.html": """{% load static %}
<!DOCTYPE html>
<html>
<head>
    <title>{{ title }}</title>
    <link rel="stylesheet" href="{% static 'css/style.css' %}">
</head>
<body>
    <h1>{{ title }}</h1>
    <script src="{% static 'js/app.js' %}"></script>
</body>
</html>""",
                                },
                            ),
                        ],
                    },
                },
            ],
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    # A file, not :memory:, so that threads share the database.
                    # Concurrent workers sharing one SQLite file fail with
                    # locked-database, UNIQUE and FK errors, so each has its own.
                    "NAME": os.path.join(config._bolt_db_dir, "db.sqlite3"),
                }
            },
            USE_TZ=True,
            LANGUAGE_CODE="en-us",
            TIME_ZONE="UTC",
            USE_I18N=True,
            STATIC_URL="/static/",
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        )
        # Setup Django apps so ExceptionReporter works
        django.setup()


@pytest.fixture(scope="session")
def django_db_setup(django_db_blocker):
    """
    Ensure database migrations are run before any tests that use the database.
    This creates the auth_user table and other Django core tables.
    Also creates test model tables (Article, etc.).

    Note: We skip the default django_db_setup to have better control over test database.
    """
    import os  # noqa: PLC0415

    from django.conf import settings  # noqa: PLC0415
    from django.core.management import call_command  # noqa: PLC0415
    from django.db import connection  # noqa: PLC0415

    with django_db_blocker.unblock():
        # Ensure test database directory exists
        db_path = settings.DATABASES["default"]["NAME"]
        if db_path and db_path != ":memory:":
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)

        # Run migrations to create all necessary tables
        call_command("migrate", "--run-syncdb", verbosity=0)

        # Create test model tables manually since they're not in migrations
        # migrate --run-syncdb can already create them; create only the missing ones.
        with connection.schema_editor() as schema_editor:
            from .test_models import (  # noqa: PLC0415
                Article,
                Author,
                BlogPost,
                Comment,
                Document,
                Tag,
                User,
                UserProfile,
            )

            models = [Article, Author, Tag, BlogPost, Comment, User, UserProfile, Document]
            for model in models:
                # Check if table already exists
                if model._meta.db_table not in connection.introspection.table_names():
                    schema_editor.create_model(model)


def pytest_unconfigure(config):
    """Remove the database directory of this test process."""
    db_dir = getattr(config, "_bolt_db_dir", None)
    if db_dir:
        shutil.rmtree(db_dir, ignore_errors=True)
