"""
Tests that request.user loading runs on the framework's bounded ORM pool.

Loading `request.user` issues a Django query, so it is ORM work and belongs
on the same bounded, vendor-aware executor as every other framework-initiated
query (`concurrency.run_in_orm_executor`). That pool exists because SQLite
throughput scales inversely with connection count — a wide pool contends on
the database file lock — and because each pool thread holds its own long-lived
connection, so the connection budget must be tunable in one place via
`DJANGO_BOLT_ORM_THREADS`.

A private user-loading pool silently escapes that budget: it opens its own
connections, ignores the env override, and reintroduces exactly the
file-lock contention the ORM pool is sized to avoid.
"""

from __future__ import annotations

import concurrent.futures
import threading

import pytest
from django.contrib.auth import get_user_model

from django_bolt import concurrency
from django_bolt.auth import JWTAuthentication, user_loader
from django_bolt.auth.user_loader import default_django_user_loader, resolve_user_loader

# Loads are asserted from other threads, which cannot see rows held open in an
# uncommitted test transaction — these tests need real committed data.
pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()

ORM_THREAD_PREFIX = "bolt_orm"


@pytest.fixture
def fresh_orm_executor(monkeypatch):
    """Give each test its own ORM pool so thread budgets are observable.

    The executor is a module-level singleton built on first use; tests that
    assert on its size must build it themselves rather than inherit whatever
    an earlier test created.
    """

    def _build(workers: int | None = None) -> concurrent.futures.ThreadPoolExecutor:
        if workers is None:
            monkeypatch.delenv("DJANGO_BOLT_ORM_THREADS", raising=False)
        else:
            monkeypatch.setenv("DJANGO_BOLT_ORM_THREADS", str(workers))
        concurrency._orm_executor = None
        return concurrency._get_orm_executor()

    previous = concurrency._orm_executor
    concurrency._orm_executor = None
    try:
        yield _build
    finally:
        created = concurrency._orm_executor
        if created is not None and created is not previous:
            created.shutdown(wait=False)
        concurrency._orm_executor = previous


class RecordingJWTAuth(JWTAuthentication):
    """Backend whose user load reports the thread it ran on and peak concurrency."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self.threads: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()

    def get_user_sync(self, user_id):
        with self._lock:
            self.threads.append(threading.current_thread().name)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.entered.release()
        try:
            # Held open only by the concurrency test, which needs both loads
            # to be in flight at once before either returns.
            self.release.wait(timeout=5)
            return super().get_user_sync(user_id)
        finally:
            with self._lock:
                self.in_flight -= 1


def _make_user(username: str = "orm-pool"):
    return User.objects.create(username=username, email=f"{username}@example.com", password="x")


def test_default_user_loader_runs_on_orm_executor(fresh_orm_executor, monkeypatch):
    """The no-backend default loader (Rust session auth) uses the ORM pool."""
    fresh_orm_executor()
    user = _make_user("default-loader")

    seen: list[str] = []
    real_pk_load = user_loader.load_user_by_pk_sync

    def recording_pk_load(model, user_id):
        seen.append(threading.current_thread().name)
        return real_pk_load(model, user_id)

    monkeypatch.setattr(user_loader, "load_user_by_pk_sync", recording_pk_load)

    loaded = default_django_user_loader(str(user.pk), None, True)
    assert loaded is not None
    assert loaded.pk == user.pk

    assert seen, "the pk query never ran"
    assert seen[0].startswith(ORM_THREAD_PREFIX), f"default user load ran on {seen[0]!r}; expected the shared ORM pool."


def test_backend_user_loader_runs_on_orm_executor(fresh_orm_executor):
    """A backend's get_user_sync is dispatched to the ORM pool, not a private one."""
    fresh_orm_executor()
    user = _make_user("backend-loader")

    backend = RecordingJWTAuth(secret="user-loader-orm-pool-test-secret", algorithms=["HS256"])
    backend.release.set()
    loader = resolve_user_loader(backend)
    assert loader is not None

    loaded = loader(str(user.pk), None, True)
    assert loaded is not None
    assert loaded.pk == user.pk

    assert backend.threads, "get_user_sync never ran"
    assert backend.threads[0].startswith(ORM_THREAD_PREFIX), (
        f"user load ran on {backend.threads[0]!r}; expected the shared ORM pool. "
        "A private user-loading pool escapes DJANGO_BOLT_ORM_THREADS and the "
        "per-thread database connection budget."
    )


def test_user_loading_respects_orm_thread_budget(fresh_orm_executor):
    """DJANGO_BOLT_ORM_THREADS caps user loads too, not just QuerySet evaluation."""
    fresh_orm_executor(workers=1)
    user = _make_user("budget")

    backend = RecordingJWTAuth(secret="user-loader-orm-pool-test-secret", algorithms=["HS256"])
    loader = resolve_user_loader(backend)

    # Two loads requested at once against a one-thread budget must serialize:
    # the second cannot enter get_user_sync while the first is parked there.
    callers = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        pending = [callers.submit(loader, str(user.pk), None, True) for _ in range(2)]
        assert backend.entered.acquire(timeout=5), "no user load started"
        # A wide pool would admit the second load here; a one-thread budget
        # cannot until the first returns.
        second_entered = backend.entered.acquire(timeout=0.5)
        backend.release.set()
        assert [f.result(timeout=5) is not None for f in pending] == [True, True]
    finally:
        backend.release.set()
        callers.shutdown(wait=False)

    assert not second_entered, (
        f"{backend.max_in_flight} user loads ran concurrently under "
        "DJANGO_BOLT_ORM_THREADS=1; the user loader is not using the ORM pool."
    )
    assert backend.max_in_flight == 1
    assert len(set(backend.threads)) == 1


def test_user_load_from_orm_thread_runs_inline(fresh_orm_executor, monkeypatch):
    """A user load already on an ORM thread runs there, not via a second hand-off.

    `request.user` is a SimpleLazyObject carrying the route's async-context
    flag, so it can be forced during work that already runs on the ORM pool
    (a serializer enc_hook touching the user while a QuerySet evaluates).
    Submitting back into the pool from its own worker waits on a slot the
    caller is holding — under the SQLite default of one thread that never
    resolves, so the query must run inline instead.

    Asserted by thread identity rather than by blocking: a regression here
    deadlocks, and a test that hangs CI is worse than one that fails.
    """
    fresh_orm_executor(workers=1)
    user = _make_user("reentrant")

    seen: list[str] = []
    real_pk_load = user_loader.load_user_by_pk_sync

    def recording_pk_load(model, user_id):
        seen.append(threading.current_thread().name)
        return real_pk_load(model, user_id)

    monkeypatch.setattr(user_loader, "load_user_by_pk_sync", recording_pk_load)

    def resolve_inside_pool():
        assert concurrency.in_orm_executor_thread()
        return threading.current_thread().name, default_django_user_loader(str(user.pk), None, True)

    pool_thread, loaded = concurrency._get_orm_executor().submit(resolve_inside_pool).result(timeout=5)

    assert loaded is not None
    assert loaded.pk == user.pk
    assert seen == [pool_thread], (
        f"query ran on {seen!r} instead of inline on {pool_thread!r}; "
        "re-submitting into the ORM pool from its own worker deadlocks a one-thread pool"
    )
