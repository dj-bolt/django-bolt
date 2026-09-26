"""A broken database connection must not poison its executor thread.

Bolt keeps Django connections open across requests (no
``close_old_connections``). A connection that died stays on its thread, so
every later request on that thread must fail unless the framework drops it
on the error path.

Two tiers, both in ``concurrency._call_guarded``:

- Always: a pool call that raises drops the unusable connections of its
  thread. The request that met the dead connection fails, the next one
  reconnects.
- With ``CONN_MAX_AGE`` or ``CONN_HEALTH_CHECKS`` set (the
  ``_check_before_call`` gate): the same check also runs before each call.
  Django's health check then finds a dead connection before the query, and
  no request fails.

``test_dead_connection_recovery_server_integration.py`` runs the user-loading
path against PostgreSQL on a real server.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import time

import jwt
import pytest
from django.contrib.auth.models import User
from django.db import connection, connections
from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteWrapper

from django_bolt import BoltAPI, concurrency
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.testing import TestClient

SECRET = "broken-connection-recovery-secret-key-32b"


def _sqlite_is_usable(self) -> bool:
    # Django's sqlite backend hard-codes ``is_usable`` to True. Ping for real,
    # as the networked backends do, so a dead connection is detected.
    try:
        self.connection.execute("SELECT 1")
    except sqlite3.Error:
        return False
    return True


@pytest.fixture
def single_thread_pool(monkeypatch):
    """Pin both pools to one thread so consecutive requests share a connection."""
    monkeypatch.setattr(SQLiteWrapper, "is_usable", _sqlite_is_usable)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="bolt_test")
    monkeypatch.setattr(concurrency, "_default_executor", pool)
    orm_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="bolt_test_orm", initializer=concurrency._mark_orm_thread
    )
    monkeypatch.setattr(concurrency, "_orm_executor", orm_pool)
    yield
    pool.shutdown(wait=True)
    orm_pool.shutdown(wait=True)


def _kill_then_query() -> None:
    # Close the DBAPI connection behind Django's back, then use it.
    connection.ensure_connection()
    connection.connection.close()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


def _select_one() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        return cursor.fetchone()[0]


@pytest.mark.django_db(transaction=True)
def test_blocking_sync_handler_recovers_after_dead_connection(single_thread_pool):
    api = BoltAPI()

    # ``time.sleep`` marks both handlers as blocking, so they run on the pool.
    @api.get("/break")
    def break_connection():
        time.sleep(0)
        _kill_then_query()
        return {"ok": True}

    @api.get("/ping")
    def ping():
        time.sleep(0)
        return {"value": _select_one()}

    with TestClient(api) as client:
        assert client.get("/break").status_code == 500
        response = client.get("/ping")
        assert response.status_code == 200
        assert response.json()["value"] == 1


@pytest.mark.django_db(transaction=True)
def test_orm_executor_recovers_after_dead_connection(single_thread_pool):
    api = BoltAPI()

    @api.get("/break")
    async def break_connection():
        await concurrency.run_in_orm_executor(_kill_then_query)
        return {"ok": True}

    @api.get("/ping")
    async def ping():
        return {"value": await concurrency.run_in_orm_executor(_select_one)}

    with TestClient(api) as client:
        assert client.get("/break").status_code == 500
        response = client.get("/ping")
        assert response.status_code == 200
        assert response.json()["value"] == 1


# --- The user-loading path and the _check_before_call gate ------------------


class _DeadDbapiConnection:
    """A DBAPI connection whose server side went away.

    A closed sqlite3 connection raises ``ProgrammingError``, which
    ``get_user_sync`` treats as a lookup failure and swallows. A networked
    backend raises ``OperationalError`` for a dead connection, and that is the
    error the user-loading path re-raises, so this stand-in raises it too.
    """

    def cursor(self, *args, **kwargs):
        raise sqlite3.OperationalError("the connection is closed")

    execute = cursor

    def close(self) -> None:
        pass


def _kill_silently() -> None:
    """Replace the connection of this thread without using it: Django records no error.

    The thread keeps its connection across tests. A new connection takes
    the settings of this test (``health_check_enabled`` is read at connect time).
    """
    connection.close()
    connection.ensure_connection()
    connection.connection.close()
    connection.connection = _DeadDbapiConnection()


def _make_token(user_id: int) -> str:
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + 3600}, SECRET, algorithm="HS256")


def _user_api() -> BoltAPI:
    api = BoltAPI()

    @api.get("/kill")
    async def kill():
        # No await: the sync fast path runs this on the thread that also serves /me.
        concurrency.run_orm_blocking(_kill_silently)
        return {"killed": True}

    @api.get("/me", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    async def me(request):
        # The lazy load runs get_user_sync on this thread through run_orm_blocking.
        return {"username": request.user.username}

    return api


@pytest.fixture
def pooled_user_headers() -> dict[str, str]:
    user = User.objects.create_user(username="pooled")
    return {"Authorization": f"Bearer {_make_token(user.id)}"}


@pytest.fixture
def database_settings(request, monkeypatch):
    """Apply the connection settings of the parameter and run the gate again."""
    monkeypatch.setattr(concurrency, "_check_before_call", concurrency._check_before_call)
    for key, value in request.param.items():
        monkeypatch.setitem(connections.settings["default"], key, value)
    concurrency.configure_connection_checks()
    return request.param


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("database_settings", "first_status_after_kill"),
    [
        # Gate off: only the error path drops the connection, so the request that meets it fails.
        pytest.param({}, 500, id="default"),
        # Gate on, no health check: the check before the call sees no error and a young connection.
        pytest.param({"CONN_MAX_AGE": 600}, 500, id="max-age-only"),
        # Gate on with health check: the dead connection is replaced before the query.
        # CONN_MAX_AGE must be positive here. With the default of 0, Django closes
        # the connection as obsolete on the same check, which would hide the health check.
        pytest.param({"CONN_MAX_AGE": 600, "CONN_HEALTH_CHECKS": True}, 200, id="health-checks"),
    ],
    indirect=["database_settings"],
)
def test_user_load_on_the_orm_pool_after_dead_connection(
    single_thread_pool, database_settings, pooled_user_headers, first_status_after_kill
):
    """The lazy ``request.user`` load runs on the reading thread through ``run_orm_blocking``.

    ``/kill`` replaces the connection of that thread without using it. The
    settings decide whether the check before the call can find it. On every
    setting, the request after the failure reconnects. Without the drop in
    ``_call_guarded``, every later request on that thread fails.
    """
    with TestClient(_user_api()) as client:
        assert client.get("/me", headers=pooled_user_headers).json() == {"username": "pooled"}
        assert client.get("/kill").json() == {"killed": True}

        assert client.get("/me", headers=pooled_user_headers).status_code == first_status_after_kill
        response = client.get("/me", headers=pooled_user_headers)
        assert response.status_code == 200
        assert response.json() == {"username": "pooled"}


@pytest.mark.parametrize(
    ("database", "expected"),
    [
        ({}, False),
        ({"CONN_MAX_AGE": 60}, True),
        # An unlimited age has nothing to age out.
        ({"CONN_MAX_AGE": None}, False),
        ({"CONN_HEALTH_CHECKS": True}, True),
    ],
)
def test_check_before_call_gate_follows_the_database_settings(monkeypatch, database, expected):
    monkeypatch.setattr(concurrency, "_check_before_call", False)
    for key, value in database.items():
        monkeypatch.setitem(connections.settings["default"], key, value)

    concurrency.configure_connection_checks()

    assert concurrency._check_before_call is expected
