"""Pytest config for bolt-mcp: configure Django before importing django_bolt."""

from __future__ import annotations

import os
import tempfile

import django
import pytest
from django.conf import settings
from django.core.management import call_command

# A temp *file* DB (not ``:memory:``): the OAuth Authorization Server tests touch the DB
# from request handlers, which run ORM through ``sync_to_async`` on a worker thread with
# its own SQLite connection. ``:memory:`` is per-connection, so that connection would see
# an empty database; a file is shared across connections/threads in the process.
_DB_PATH = os.path.join(tempfile.gettempdir(), "bolt_mcp_test.sqlite3")

if not settings.configured:
    if os.path.exists(_DB_PATH):
        os.remove(_DB_PATH)
    settings.configure(
        DEBUG=True,
        SECRET_KEY="bolt-mcp-test-secret-key-at-least-32-bytes-long-0123456789",
        ALLOWED_HOSTS=["*"],
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django_bolt",
            "bolt_mcp.oauth",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": _DB_PATH,
                "OPTIONS": {"timeout": 30},
            }
        },
        MIDDLEWARE=[],
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
    )
    django.setup()

    # Create the OAuth + auth + session tables in the shared file DB once per session.
    call_command("migrate", run_syncdb=True, verbosity=0)


@pytest.fixture(autouse=True, scope="session")
def _allow_db_access(request):
    """Lift pytest-django's DB guard for the whole session, if the plugin is present.

    These tests manage their own migrated file database (see above) and read it from
    request-handler worker threads via ``sync_to_async`` — a connection pytest-django's
    transaction-based ``django_db`` fixture would not cover. We just unblock raw access
    rather than adopt its test-DB/transaction model.
    """
    try:
        blocker = request.getfixturevalue("django_db_blocker")
    except pytest.FixtureLookupError:
        yield
        return
    with blocker.unblock():
        yield
