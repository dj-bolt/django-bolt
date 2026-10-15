"""A broken database connection must not poison its executor thread.

Bolt keeps Django connections open across requests (no
``close_old_connections``). A connection that died stays on its thread, so
every later request on that thread must fail unless the framework drops it
on the error path.

Three tiers, all in ``concurrency._call_guarded``:

- Always: a pool call that raises drops the unusable connections of its
  thread. The request that met the dead connection fails, the next one
  reconnects.
- Always: when the dropped connection did not answer, every thread closes
  its older connections before its next call (a lane, when its request
  starts). A database restart then fails one call, not one on each thread.
- With ``CONN_MAX_AGE`` or ``CONN_HEALTH_CHECKS`` set (the
  ``_check_before_call`` gate): the same check also runs before each call.
  Django's health check then finds a dead connection before the query, and
  no request fails.

Django's async ORM API (``aget``, ``acount``, ``async for``) is
``sync_to_async(thread_sensitive=True)``. In a route without Django
middleware, Bolt runs these calls on the ORM pool, so the same tiers
apply to them.

``test_dead_connection_recovery_server_integration.py`` runs the user-loading
path against PostgreSQL on a real server.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sqlite3
import threading
import time

import jwt
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth.models import User
from django.db import DatabaseError, connection, connections
from django.db.backends.signals import connection_created
from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteWrapper

from django_bolt import BoltAPI, concurrency
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.testing import TestClient
from tests.thread_pools import shutdown_closing_connections

SECRET = "broken-connection-recovery-secret-key-32b"


def _sqlite_is_usable(self) -> bool:
    # Django's sqlite backend hard-codes ``is_usable`` to True. Ping for real,
    # as the networked backends do, so a dead connection is detected.
    try:
        self.connection.execute("SELECT 1")
    except sqlite3.Error:
        return False
    return True


def _orm_pool(workers: int) -> concurrent.futures.ThreadPoolExecutor:
    """An ORM pool as ``concurrency._get_orm_executor`` builds it, with a fixed size."""
    return concurrent.futures.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="bolt_test_orm", initializer=concurrency._mark_orm_thread
    )


@pytest.fixture
def single_thread_pool(monkeypatch):
    """Pin both pools to one thread so consecutive requests share a connection."""
    monkeypatch.setattr(SQLiteWrapper, "is_usable", _sqlite_is_usable)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="bolt_test")
    monkeypatch.setattr(concurrency, "_default_executor", pool)
    orm_pool = _orm_pool(1)
    monkeypatch.setattr(concurrency, "_orm_executor", orm_pool)
    yield
    shutdown_closing_connections(pool)
    shutdown_closing_connections(orm_pool)


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


@pytest.mark.django_db(transaction=True)
def test_sync_to_async_recovers_after_dead_connection(single_thread_pool):
    """Django's async ORM API (``aget``, ``acount``, ``async for``) is ``sync_to_async``.

    asgiref runs these calls on its own thread, outside the pools. A dead
    connection on that thread must not fail every later async ORM call.
    """
    api = BoltAPI()

    @api.get("/break")
    async def break_connection():
        await sync_to_async(_kill_then_query)()
        return {"ok": True}

    @api.get("/ping")
    async def ping():
        return {"value": await sync_to_async(_select_one)()}

    with TestClient(api) as client:
        assert client.get("/break").status_code == 500
        response = client.get("/ping")
        assert response.status_code == 200
        assert response.json()["value"] == 1


@pytest.mark.django_db(transaction=True)
def test_sync_to_async_calls_of_different_requests_run_in_parallel(monkeypatch):
    """Thread-sensitive calls of plain async routes use the ORM pool, not one shared thread."""
    pool = _orm_pool(4)
    monkeypatch.setattr(concurrency, "_orm_executor", pool)
    api = BoltAPI()

    def sleep_on_pool() -> int:
        time.sleep(0.2)
        return threading.get_ident()

    @api.get("/slow")
    async def slow():
        return {"thread": await sync_to_async(sleep_on_pool)()}

    with TestClient(api) as client, concurrent.futures.ThreadPoolExecutor(max_workers=4) as requests:
        started = time.perf_counter()
        responses = list(requests.map(lambda _: client.get("/slow"), range(4)))
        elapsed = time.perf_counter() - started
    shutdown_closing_connections(pool)

    assert all(response.status_code == 200 for response in responses)
    # One shared thread takes 4 × 0.2 s. The pool runs the four calls at the same time.
    assert elapsed < 0.5, f"4 sleeps of 0.2 s took {elapsed:.2f} s"
    assert len({response.json()["thread"] for response in responses}) > 1


@pytest.mark.django_db(transaction=True)
def test_nested_orm_handoff_inside_a_thread_sensitive_call_with_one_orm_thread(single_thread_pool):
    """A thread-sensitive call holds the one ORM thread. A nested hand-off must not wait for it.

    ``sync_to_async`` runs ``call_the_loop`` on the ORM pool. It calls
    ``async_to_sync``, so ``hand_off_a_query`` runs on the loop of the request
    while the pool thread waits. With one ORM thread, that hand-off must take
    another executor, or the request never answers.
    """

    async def hand_off_a_query() -> int:
        return await concurrency.run_in_orm_executor(_select_one)

    def call_the_loop() -> int:
        return async_to_sync(hand_off_a_query)()

    api = BoltAPI()

    @api.get("/nested")
    async def nested():
        return {"value": await sync_to_async(call_the_loop)()}

    with TestClient(api) as client:
        result: list = []
        worker = threading.Thread(target=lambda: result.append(client.get("/nested")), daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), "the request did not answer in 5 s: the nested hand-off waits for its own thread"
        assert result[0].status_code == 200
        assert result[0].json() == {"value": 1}


# --- One dead connection retires the connections of every thread -----------


def _isolate_generations(monkeypatch) -> None:
    """A connection that dies in a test must not retire the connections of later tests."""
    monkeypatch.setattr(concurrency, "_generations", dict(concurrency._generations))
    monkeypatch.setattr(concurrency, "_generation_epoch", concurrency._generation_epoch)
    monkeypatch.setattr(concurrency, "_pool_epochs", dict(concurrency._pool_epochs))


@pytest.fixture
def two_orm_threads(monkeypatch):
    monkeypatch.setattr(SQLiteWrapper, "is_usable", _sqlite_is_usable)
    # A connection that dies in a test must not retire the connections of later tests.
    _isolate_generations(monkeypatch)
    pool = _orm_pool(2)
    monkeypatch.setattr(concurrency, "_orm_executor", pool)
    yield
    shutdown_closing_connections(pool)


def _query_a_missing_table() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM bolt_no_such_table")


def _query_a_missing_table_with_autocommit_off() -> None:
    # The call ends with autocommit off, so Django closes the connection
    # before it tests whether the connection answers.
    connection.set_autocommit(False)
    _query_a_missing_table()


def _orm_threads_api() -> BoltAPI:
    api = BoltAPI()
    queries = {
        "select": _select_one,
        "kill": _kill_silently,
        "missing-table": _query_a_missing_table,
        "missing-table-no-autocommit": _query_a_missing_table_with_autocommit_off,
    }

    @api.get("/orm-threads/{query}")
    async def orm_threads(query: str, count: int):
        """Run ``query`` on ``count`` ORM pool threads at the same time.

        The barrier holds each call until all of them run, so each call has a
        thread of its own. Each result is ``"ok"`` or the name of the error.
        """
        barrier = threading.Barrier(count, timeout=5)

        def run() -> str:
            barrier.wait()
            queries[query]()
            return "ok"

        results = await asyncio.gather(
            *(concurrency.run_in_orm_executor(run) for _ in range(count)), return_exceptions=True
        )
        return [result if isinstance(result, str) else type(result).__name__ for result in results]

    return api


@pytest.mark.django_db(transaction=True)
def test_a_dead_connection_retires_the_connections_of_every_thread(two_orm_threads):
    """A database restart kills every connection, but each thread sees this only when it uses its own.

    The first call that meets a dead connection fails. The calls after it
    reconnect on every thread, not only on the thread of the failure. Without
    that, each thread fails one call of its own.
    """
    with TestClient(_orm_threads_api()) as client:
        assert client.get("/orm-threads/select?count=2").json() == ["ok", "ok"]
        assert client.get("/orm-threads/kill?count=2").json() == ["ok", "ok"]

        assert client.get("/orm-threads/select?count=1").json() == ["OperationalError"]
        assert client.get("/orm-threads/select?count=2").json() == ["ok", "ok"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("query", ["missing-table", "missing-table-no-autocommit"])
def test_an_error_on_a_connection_that_still_answers_retires_nothing(two_orm_threads, query):
    """A failed query is not a dead connection. The other thread keeps its connection.

    The thread of the error drops its own connection as obsolete: the test
    database has the default ``CONN_MAX_AGE`` of 0. Thus one thread reconnects.
    """
    opened: list[object] = []

    def count(sender, connection, **kwargs) -> None:
        opened.append(connection)

    with TestClient(_orm_threads_api()) as client:
        assert client.get("/orm-threads/select?count=2").json() == ["ok", "ok"]
        connection_created.connect(count)
        try:
            assert client.get(f"/orm-threads/{query}?count=1").json() == ["OperationalError"]
            assert client.get("/orm-threads/select?count=2").json() == ["ok", "ok"]
        finally:
            connection_created.disconnect(count)

    assert len(opened) == 1


@pytest.mark.django_db(transaction=True)
def test_a_lane_reconnects_when_a_request_starts_after_another_thread_met_a_dead_connection(monkeypatch):
    """A lane runs its checks in ``open_lane_request``, not before each call."""
    monkeypatch.setattr(SQLiteWrapper, "is_usable", _sqlite_is_usable)
    _isolate_generations(monkeypatch)
    lane, pool = _orm_pool(1), _orm_pool(1)
    monkeypatch.setattr(concurrency, "_orm_executor", pool)
    try:
        lane.submit(_kill_silently).result()
        with TestClient(_orm_threads_api()) as client:
            assert client.get("/orm-threads/kill?count=1").json() == ["ok"]
            assert client.get("/orm-threads/select?count=1").json() == ["OperationalError"]

        lane.submit(concurrency.open_lane_request).result()

        assert lane.submit(_select_one).result() == 1
    finally:
        shutdown_closing_connections(lane)
        shutdown_closing_connections(pool)


@pytest.mark.django_db(transaction=True)
def test_one_event_makes_one_reconnect_wave(monkeypatch):
    """A thread that meets a dead connection of an event that already advanced the generation advances nothing.

    Thread A finds the first dead connection, and thread C reconnects before
    its next call. Thread B started its call before the advance, so it meets
    its dead connection later. Without the check, B advances the generation a
    second time, and C closes the connection that it has just opened.
    """
    monkeypatch.setattr(SQLiteWrapper, "is_usable", _sqlite_is_usable)
    _isolate_generations(monkeypatch)
    a, b, c = _orm_pool(1), _orm_pool(1), _orm_pool(1)
    b_in_call = threading.Event()
    b_may_query = threading.Event()

    def wait_then_select() -> int:
        # The check before the call has run: B keeps its dead connection.
        b_in_call.set()
        b_may_query.wait(5)
        return _select_one()

    opened_on_c: list[object] = []

    def count_on_c(sender, connection, **kwargs) -> None:
        if threading.get_ident() == c_thread:
            opened_on_c.append(connection)

    try:
        for pool in (a, b, c):
            assert pool.submit(concurrency._call_guarded, _select_one).result() == 1
        c_thread = c.submit(threading.get_ident).result()
        for pool in (a, b, c):
            pool.submit(_kill_silently).result()

        late = b.submit(concurrency._call_guarded, wait_then_select)
        assert b_in_call.wait(5), "thread B did not start its call"
        with pytest.raises(DatabaseError):
            a.submit(concurrency._call_guarded, _select_one).result()

        connection_created.connect(count_on_c)
        try:
            assert c.submit(concurrency._call_guarded, _select_one).result() == 1
            b_may_query.set()
            with pytest.raises(DatabaseError):
                late.result(5)
            assert c.submit(concurrency._call_guarded, _select_one).result() == 1
        finally:
            connection_created.disconnect(count_on_c)
    finally:
        b_may_query.set()
        for pool in (a, b, c):
            shutdown_closing_connections(pool)

    assert len(opened_on_c) == 1, "thread C reconnected again after the second report of the same event"


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
