"""TestClient shares the database connection of the test that started it.

Handlers run on framework threads. Django gives each thread its own connection.
Thus a test that holds an open transaction made rows that no handler could read.
`TestClient` now lends its connection to those threads while it is open. Thus
plain `TestCase` and plain `django_db` work. `transaction=True` is no longer a
condition of testing a handler that reads the database (issue #276).
"""

from __future__ import annotations

import asyncio
import threading
import warnings

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, connections

from django_bolt import BoltAPI
from django_bolt.testing import AsyncTestClient, TestClient
from django_bolt.testing import client as client_module
from django_bolt.testing.client import BoltTestClientWarning
from django_bolt.testing.dbshare import DEFAULT_LOCK_TIMEOUT, SharedTestConnectionError, lock_timeout

User = get_user_model()


def _make_api() -> BoltAPI:
    api = BoltAPI()

    @api.get("/users")
    async def list_users():
        return User.objects.values("username")

    @api.get("/count")
    async def count_users():
        return {"n": await User.objects.acount()}

    @api.post("/users")
    async def create_user():
        user = await User.objects.acreate(username="made-by-handler", email="h@example.com")
        return {"id": user.pk}

    @api.get("/ping")
    async def ping():
        return {"ok": True}

    return api


# ── the behaviour the sharing exists for ──────────────────────────────────────
@pytest.mark.django_db  # NOTE: no transaction=True
def test_handler_reads_a_row_the_test_has_not_committed():
    assert connection.in_atomic_block, "precondition: plain django_db is transactional"
    User.objects.create(username="uncommitted", email="u@example.com")

    with TestClient(_make_api()) as client:
        response = client.get("/users")

    assert response.status_code == 200
    assert [row["username"] for row in response.json()] == ["uncommitted"]


@pytest.mark.django_db
def test_a_row_a_handler_writes_is_visible_to_the_test():
    with TestClient(_make_api()) as client:
        assert client.post("/users").status_code == 200

    assert User.objects.filter(username="made-by-handler").exists()


@pytest.mark.django_db
def test_rollback_isolation_still_removes_the_rows():
    """The previous test wrote through a handler; none of it may survive."""
    assert User.objects.count() == 0


# ── the guard that makes sharing safe ─────────────────────────────────────────
@pytest.mark.django_db
def test_concurrent_database_work_is_serialized_not_corrupted():
    """A thread of the test querying while requests run must not mix rows.

    Without the cursor lock this returns rows of the wrong shape, which the ORM
    reports as ``IndexError: list index out of range``.
    """
    for i in range(20):
        User.objects.create(username=f"u{i}", email=f"u{i}@example.com")

    errors: list[str] = []
    stop = threading.Event()

    def churn():
        while not stop.is_set():
            try:
                User.objects.count()
            except Exception as exc:  # noqa: BLE001 - any failure is the finding
                errors.append(repr(exc))

    with TestClient(_make_api()) as client:
        threads = [threading.Thread(target=churn) for _ in range(4)]
        for thread in threads:
            thread.start()
        statuses = {client.get("/count").status_code for _ in range(100)}
        stop.set()
        for thread in threads:
            thread.join()

    assert errors == []
    assert statuses == {200}


@pytest.mark.django_db
def test_a_request_inside_an_iterator_loop_works():
    """The test parks its locks while it waits, thus this must not deadlock.

    A thread inside ``client.get()`` is blocked in the Rust pipeline and runs no
    query, thus the cursor it holds open is idle. It gives its locks back for
    the time of the request, and takes them again after.
    """
    for i in range(12):
        User.objects.create(username=f"u{i}", email=f"u{i}@example.com")

    seen = []
    with TestClient(_make_api()) as client:
        for user in User.objects.iterator(chunk_size=2):
            seen.append(user.username)
            assert client.get("/count").json()["n"] == 12

    assert len(seen) == 12


@pytest.mark.django_db
def test_a_cursor_held_by_another_thread_raises_a_named_error(monkeypatch):
    """A holder that is not waiting for the response cannot be parked."""
    monkeypatch.setenv("DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT", "0.5")
    for i in range(8):
        User.objects.create(username=f"u{i}", email=f"u{i}@example.com")

    holding = threading.Event()
    release = threading.Event()

    def hold_a_cursor():
        # A thread of the test that iterates, and never reaches the end.
        for _ in User.objects.iterator(chunk_size=1):
            holding.set()
            release.wait(5)
            return

    with TestClient(_make_api()) as client:
        holder = threading.Thread(target=hold_a_cursor)
        holder.start()
        assert holding.wait(5)
        with pytest.raises(SharedTestConnectionError) as excinfo:
            client.get("/count")
        release.set()
        holder.join(5)

    message = str(excinfo.value)
    assert "holds a cursor open" in message
    assert "share_db_connection=False" in message
    assert "DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT" in message


# ── switching it off ──────────────────────────────────────────────────────────
@pytest.fixture
def fresh_advisory_latch():
    """The advisory fires one time in each process; each test needs a new latch."""
    client_module._atomic_block_warning_emitted.clear()
    yield
    client_module._atomic_block_warning_emitted.clear()


@pytest.mark.django_db
def test_share_db_connection_false_restores_the_advisory(fresh_advisory_latch):
    with (
        pytest.warns(BoltTestClientWarning, match="TransactionTestCase"),
        TestClient(_make_api(), share_db_connection=False) as client,
    ):
        assert client.get("/ping").status_code == 200


@pytest.mark.django_db
def test_the_advisory_is_emitted_only_once_per_process(fresh_advisory_latch, recwarn):
    """A suite that opts out in many tests must not be flooded."""

    def advisories():
        return [w for w in recwarn.list if issubclass(w.category, BoltTestClientWarning)]

    with TestClient(_make_api(), share_db_connection=False):
        pass
    assert len(advisories()) == 1

    with TestClient(_make_api(), share_db_connection=False):
        pass
    assert len(advisories()) == 1


def test_no_advisory_without_any_database_access(fresh_advisory_latch, recwarn):
    """No database is involved at all, thus there is nothing to warn about."""
    with TestClient(_make_api(), share_db_connection=False) as client:
        assert client.get("/ping").status_code == 200

    assert [w for w in recwarn.list if issubclass(w.category, BoltTestClientWarning)] == []


@pytest.mark.django_db
def test_async_client_does_not_share_or_warn(fresh_advisory_latch):
    """AsyncTestClient cannot see the transaction, thus it does neither.

    Django keeps connections in a thread-critical ``asgiref`` Local. Code in an
    event loop gets its own connection object, which is not in the atomic block
    that pytest-django opened on this thread. This test records that fact, so
    that neither sharing nor an advisory is built on a check that cannot see the
    transaction of the test.
    """
    assert connection.in_atomic_block, "precondition: plain django_db is transactional"

    async def aliases_seen_by_the_loop():
        return [conn.alias for conn in connections.all(initialized_only=True) if conn.in_atomic_block]

    assert asyncio.run(aliases_seen_by_the_loop()) == []

    async def enter_and_exit():
        async with AsyncTestClient(_make_api()) as client:
            assert (await client.get("/ping")).status_code == 200

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        asyncio.run(enter_and_exit())

    assert [w for w in recorded if issubclass(w.category, BoltTestClientWarning)] == []


@pytest.mark.django_db(transaction=True)
def test_the_escape_hatch_serves_a_worker_that_iterates():
    """The fix the error names must work for the case that raises it.

    This is the example in the testing guide: a thread of the test iterates a
    QuerySet, which holds a cursor open, while the client serves requests.
    """
    for i in range(20):
        User.objects.create(username=f"u{i}", email=f"u{i}@example.com")

    exported: list[str] = []

    def export():
        for user in User.objects.iterator(chunk_size=2):
            exported.append(user.username)

    with TestClient(_make_api(), share_db_connection=False) as client:
        worker = threading.Thread(target=export)
        worker.start()
        statuses = {client.get("/count").status_code for _ in range(15)}
        worker.join()

    assert statuses == {200}
    assert len(exported) == 20


@pytest.mark.django_db(transaction=True)
def test_nothing_is_shared_when_the_test_commits():
    """With committed rows the handler threads need no help, thus none is given."""
    assert not connection.in_atomic_block
    store_before = connections._connections

    with TestClient(_make_api()) as client:
        assert connections._connections is store_before
        assert client.get("/ping").status_code == 200


# ── configuration and concurrent entry ────────────────────────────────────────
@pytest.mark.parametrize("bad", ["nan", "inf", "-1", "-5", "abc"])
def test_a_bad_lock_timeout_is_rejected(monkeypatch, bad):
    """Every one of these breaks the lock, thus none may pass silently.

    ``nan`` and ``-5`` make ``acquire()`` raise, ``inf`` overflows, ``-1`` means
    wait forever, and text used to fall back to the default with no report.
    """
    monkeypatch.setenv("DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT", bad)
    with pytest.raises(ValueError, match="DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT"):
        lock_timeout()


def test_an_unset_or_empty_lock_timeout_uses_the_default(monkeypatch):
    monkeypatch.delenv("DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT", raising=False)
    assert lock_timeout() == DEFAULT_LOCK_TIMEOUT
    monkeypatch.setenv("DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT", "")
    assert lock_timeout() == DEFAULT_LOCK_TIMEOUT
    monkeypatch.setenv("DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT", "0.5")
    assert lock_timeout() == 0.5


@pytest.mark.django_db
def test_a_second_thread_cannot_enter_its_own_client():
    """The shared store is process-wide, thus a second test must not join it.

    Two threads that each enter a client would lend the connection of one test
    to the handlers of the other, which mixes their rows. No supported runner
    runs two tests in one process at the same time, thus this is refused.
    """
    outcome: list[object] = []

    def other_test():
        try:
            with TestClient(_make_api()):
                outcome.append("entered")
        except BaseException as exc:  # noqa: BLE001 - the outcome is the finding
            outcome.append(exc)

    with TestClient(_make_api()) as client:
        assert client.get("/ping").status_code == 200
        thread = threading.Thread(target=other_test)
        thread.start()
        thread.join()

    assert len(outcome) == 1
    assert isinstance(outcome[0], SharedTestConnectionError), outcome[0]
    assert "at the same time" in str(outcome[0])


@pytest.mark.django_db
def test_nested_clients_on_one_thread_still_work():
    """Nesting on one thread is legal, and the outer client keeps working."""
    User.objects.create(username="nested", email="n@example.com")

    with TestClient(_make_api()) as outer:
        with TestClient(_make_api()) as inner:
            assert inner.get("/count").json()["n"] == 1
        assert outer.get("/count").json()["n"] == 1


# ── the client must leave Django as it found it ───────────────────────────────
@pytest.mark.django_db
def test_the_connection_store_is_restored_on_exit():
    store_before = connections._connections
    conn = connections["default"]
    sharing_before = conn._thread_sharing_count
    cursor_before = conn.cursor

    with TestClient(_make_api()) as client:
        assert connections._connections is not store_before
        assert conn._thread_sharing_count == sharing_before + 1
        assert client.get("/ping").status_code == 200

    assert connections._connections is store_before
    assert conn._thread_sharing_count == sharing_before
    assert conn.cursor == cursor_before


@pytest.mark.django_db
def test_the_store_is_restored_when_the_body_raises():
    store_before = connections._connections
    with pytest.raises(ZeroDivisionError), TestClient(_make_api()):
        raise ZeroDivisionError
    assert connections._connections is store_before
