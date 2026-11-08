"""
Tests for the thread of a request.user load.

A sync read of `request.user` blocks the thread that reads it, so the query
runs on that thread (`concurrency.run_orm_blocking`). A hop to a pool would
add two thread wakeups and block the reader all the same. An async load
(`await request.auser()`) is ORM work and uses the bounded, vendor-aware
executor of every framework-initiated query (`concurrency.run_in_orm_executor`).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
import time

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connections

from django_bolt import concurrency
from django_bolt.auth import APIKeyAuthentication, JWTAuthentication, user_loader
from django_bolt.auth.pk_loader import load_user_by_pk_sync
from django_bolt.auth.user_loader import default_django_user_loader, resolve_user_loader
from tests.thread_pools import shutdown_closing_connections

# Loads are asserted from other threads, which cannot see rows held open in an
# uncommitted test transaction — these tests need real committed data.
pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()

ORM_THREAD_PREFIX = "bolt_orm"
DEFAULT_THREAD_PREFIX = "bolt_default"


@pytest.fixture
def fresh_orm_executor(monkeypatch):
    """Give each test its own ORM pool so thread budgets are observable.

    The executor is a module-level singleton built on first use; tests that
    assert on its size must build it themselves rather than inherit whatever
    an earlier test created. Every pool built here is drained with
    ``shutdown(wait=True)`` before the fixture returns: workers hold database
    connections, and the transactional teardown truncates tables right after.
    """
    built: list[concurrent.futures.ThreadPoolExecutor] = []

    def _build(workers: int | str | None = None) -> concurrent.futures.ThreadPoolExecutor:
        if workers is None:
            monkeypatch.delenv("DJANGO_BOLT_ORM_THREADS", raising=False)
        else:
            monkeypatch.setenv("DJANGO_BOLT_ORM_THREADS", str(workers))
        concurrency._orm_executor = None
        executor = concurrency._get_orm_executor()
        built.append(executor)
        return executor

    previous = concurrency._orm_executor
    concurrency._orm_executor = None
    try:
        yield _build
    finally:
        current = concurrency._orm_executor
        if current is not None and current not in built:
            built.append(current)
        for executor in built:
            if executor is not previous:
                shutdown_closing_connections(executor)
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
            assert self.release.wait(timeout=5), "release was never set; the test parked this user load"
            return super().get_user_sync(user_id)
        finally:
            with self._lock:
                self.in_flight -= 1


class AsyncGetUserAuth(JWTAuthentication):
    """Backend with a custom async get_user. ``orm_threads`` records where the query lands."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.orm_threads: list[str] = []

    async def get_user(self, user_id, auth_context):
        def query():
            self.orm_threads.append(threading.current_thread().name)
            return load_user_by_pk_sync(User, user_id)

        return await concurrency.run_in_orm_executor(query)


def _make_user(username: str = "orm-pool"):
    return User.objects.create(username=username, email=f"{username}@example.com", password="x")


def _force_on_event_loop(loader, *args):
    """Force a lazy user on a thread with a running loop, as an async handler or middleware does.

    The loop blocks on the read, so the query runs on this thread.
    """

    async def force():
        return loader(*args)

    return asyncio.run(force())


def test_default_user_loader_runs_on_the_reading_thread(fresh_orm_executor, monkeypatch):
    """The no-backend default loader (Rust session auth) runs on the thread that reads the user."""
    fresh_orm_executor()
    user = _make_user("default-loader")

    seen: list[str] = []
    real_pk_load = user_loader.load_user_by_pk_sync

    def recording_pk_load(model, user_id):
        seen.append(threading.current_thread().name)
        return real_pk_load(model, user_id)

    monkeypatch.setattr(user_loader, "load_user_by_pk_sync", recording_pk_load)

    loaded = _force_on_event_loop(default_django_user_loader, str(user.pk), None)
    assert loaded is not None
    assert loaded.pk == user.pk

    assert seen == [threading.current_thread().name], f"default user load ran on {seen!r}; expected the reading thread."


def test_backend_user_loader_runs_on_the_reading_thread(fresh_orm_executor):
    """A backend's get_user_sync runs on the thread that reads the user, not on a pool."""
    fresh_orm_executor()
    user = _make_user("backend-loader")

    backend = RecordingJWTAuth(secret="user-loader-orm-pool-test-secret", algorithms=["HS256"])
    backend.release.set()
    loader, _ = resolve_user_loader(backend)
    assert loader is not None

    loaded = _force_on_event_loop(loader, str(user.pk), None)
    assert loaded is not None
    assert loaded.pk == user.pk

    assert backend.threads == [threading.current_thread().name], (
        f"user load ran on {backend.threads!r}; expected the reading thread."
    )


def test_user_load_from_orm_thread_runs_inline(fresh_orm_executor, monkeypatch):
    """A user load already on an ORM thread runs there, not via a second hand-off.

    `request.user` is a SimpleLazyObject, so it can be forced during work
    that already runs on the ORM pool (a serializer enc_hook touching the
    user while a QuerySet evaluates).
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
        return threading.current_thread().name, default_django_user_loader(str(user.pk), None)

    pool_thread, loaded = concurrency._get_orm_executor().submit(resolve_inside_pool).result(timeout=5)

    assert loaded is not None
    assert loaded.pk == user.pk
    assert seen == [pool_thread], (
        f"query ran on {seen!r} instead of inline on {pool_thread!r}; "
        "re-submitting into the ORM pool from its own worker deadlocks a one-thread pool"
    )


def test_run_in_orm_executor_reentry_leaves_the_callers_pool(fresh_orm_executor):
    """Reentrant ORM work leaves the caller's pool — and the caller's thread.

    User code can run an event loop on a pool worker, for example with
    ``asyncio.run``. ORM work that the loop awaits then re-enters
    ``run_in_orm_executor`` from inside the pool. Submitting back into the
    pool waits on the slot that worker holds (a deadlock on a one-thread
    pool), and running inline is impossible: the worker now has a running
    event loop, so a real query raises Django's SynchronousOnlyOperation.
    The work must cross to the default pool.

    Bounded by result(timeout=...) so a deadlock regression fails instead of
    wedging the run; the query is real so an async-unsafe regression fails
    loudly too.
    """
    fresh_orm_executor(workers=1)
    user = _make_user("nested")

    def nested_orm_work():
        return threading.current_thread().name, load_user_by_pk_sync(User, str(user.pk))

    def drive_nested_loop():
        assert concurrency.in_orm_executor_thread()
        outer = threading.current_thread().name
        inner, loaded = asyncio.run(concurrency.run_in_orm_executor(nested_orm_work))
        return outer, inner, loaded

    outer, inner, loaded = concurrency._get_orm_executor().submit(drive_nested_loop).result(timeout=10)
    assert loaded is not None
    assert loaded.pk == user.pk
    assert inner != outer
    assert inner.startswith(DEFAULT_THREAD_PREFIX), (
        f"nested ORM work ran on {inner!r}; expected the default pool — submitting "
        "into the ORM pool from one of its own workers deadlocks a one-thread pool"
    )


def test_user_forced_under_a_loop_on_an_orm_worker_runs_on_the_worker(fresh_orm_executor):
    """A blocking read under a loop on an ORM worker runs on that worker.

    The worker holds the one ORM slot. A hop into its own pool would wait
    on that slot forever. The read runs inline, so there is no hop.
    """
    fresh_orm_executor(workers=1)
    user = _make_user("nested-sync")
    seen: list[str] = []

    def recording_pk_load(model, user_id):
        seen.append(threading.current_thread().name)
        return load_user_by_pk_sync(model, user_id)

    async def force():
        return concurrency.run_orm_blocking(recording_pk_load, User, str(user.pk))

    def drive_nested_loop():
        assert concurrency.in_orm_executor_thread()
        return asyncio.run(force())

    loaded = concurrency._get_orm_executor().submit(drive_nested_loop).result(timeout=10)
    assert loaded is not None
    assert loaded.pk == user.pk
    assert seen and seen[0].startswith(ORM_THREAD_PREFIX), f"query ran on {seen!r}; expected the worker itself"


@pytest.mark.parametrize("handoff", ["async", "blocking"])
def test_async_to_sync_on_an_orm_worker_leaves_the_callers_pool(fresh_orm_executor, handoff):
    """``async_to_sync`` on an ORM worker runs its loop on a new thread, not on the worker.

    A ``get_user_sync`` that wraps an async ``get_user`` does this. The new
    thread is not a pool thread, but the worker still holds the one ORM slot.
    An async hand-off from that loop must use the default pool. A blocking
    read runs on the thread of that loop.
    """
    fresh_orm_executor(workers=1)
    user = _make_user(f"a2s-{handoff}")
    seen: list[str] = []
    loop_thread: list[str] = []

    def recording_pk_load(model, user_id):
        seen.append(threading.current_thread().name)
        try:
            return load_user_by_pk_sync(model, user_id)
        finally:
            # This can run on the loop thread of async_to_sync, which ends with
            # the test. Close its connection, so the thread does not leak it.
            connections.close_all()

    async def nested():
        loop_thread.append(threading.current_thread().name)
        if handoff == "async":
            return await concurrency.run_in_orm_executor(recording_pk_load, User, str(user.pk))
        return concurrency.run_orm_blocking(recording_pk_load, User, str(user.pk))

    def on_worker():
        assert concurrency.in_orm_executor_thread()
        return async_to_sync(nested)()

    async def drive():
        return await asyncio.wait_for(concurrency.run_in_orm_executor(on_worker), timeout=10)

    loaded = asyncio.run(drive())
    assert loaded is not None
    assert loaded.pk == user.pk
    if handoff == "async":
        assert seen and seen[0].startswith(DEFAULT_THREAD_PREFIX), f"query ran on {seen!r}; expected the default pool"
    else:
        assert seen == loop_thread, f"query ran on {seen!r}; expected the thread of the loop {loop_thread!r}"


def test_orm_executor_is_built_once_under_concurrent_first_use(fresh_orm_executor, monkeypatch):
    """Racing first callers must share one pool.

    Lazy construction is now reached from arbitrary threads forcing
    `request.user`, not just from the event loop. Two callers that both see
    `None` would each build a pool, and the loser's threads — and their
    database connections — leak outside the budget.
    """
    monkeypatch.setenv("DJANGO_BOLT_ORM_THREADS", "2")
    concurrency._orm_executor = None

    built: list[concurrent.futures.ThreadPoolExecutor] = []
    built_lock = threading.Lock()
    real_ctor = concurrent.futures.ThreadPoolExecutor

    def counting_ctor(*args, **kwargs):
        # Widen the check-then-create window deterministically. Unsynchronized
        # lazy construction is a race whether or not the interpreter happens to
        # switch threads inside those few bytecodes; holding construction open
        # makes the outcome depend on the lock rather than on scheduling luck.
        time.sleep(0.05)
        executor = real_ctor(*args, **kwargs)
        with built_lock:
            built.append(executor)
        return executor

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", counting_ctor)

    racers = 16
    start = threading.Barrier(racers)

    def first_use():
        start.wait(timeout=5)
        return concurrency._get_orm_executor()

    with real_ctor(max_workers=racers) as callers:
        pools = [f.result(timeout=10) for f in [callers.submit(first_use) for _ in range(racers)]]

    assert len({id(p) for p in pools}) == 1, "racing callers received different ORM pools"
    assert len(built) == 1, f"{len(built)} ORM pools were constructed; the loser's threads leak"


def test_default_executor_is_built_once_under_concurrent_first_use(monkeypatch):
    """Racing first callers of the default pool must share one pool.

    The default executor's lazy construction was reachable only from event
    loop threads; the user-load shim and reentrant ORM hand-off now reach it
    from arbitrary threads, so it needs the same lock as the ORM pool — two
    callers that both see `None` would each build a pool and the loser's
    threads leak.
    """
    previous = concurrency._default_executor
    concurrency._default_executor = None

    built: list[concurrent.futures.ThreadPoolExecutor] = []
    built_lock = threading.Lock()
    real_ctor = concurrent.futures.ThreadPoolExecutor

    def counting_ctor(*args, **kwargs):
        # Widen the check-then-create window deterministically, as in the ORM
        # pool race test: the outcome must depend on the lock, not on
        # scheduling luck.
        time.sleep(0.05)
        executor = real_ctor(*args, **kwargs)
        with built_lock:
            built.append(executor)
        return executor

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", counting_ctor)

    racers = 16
    start = threading.Barrier(racers)

    def first_use():
        start.wait(timeout=5)
        return concurrency._get_default_executor()

    try:
        with real_ctor(max_workers=racers) as callers:
            pools = [f.result(timeout=10) for f in [callers.submit(first_use) for _ in range(racers)]]
    finally:
        for executor in built:
            if executor is not concurrency._default_executor:
                shutdown_closing_connections(executor)
        if concurrency._default_executor is not previous and concurrency._default_executor is not None:
            shutdown_closing_connections(concurrency._default_executor)
        concurrency._default_executor = previous

    assert len({id(p) for p in pools}) == 1, "racing callers received different default pools"
    assert len(built) == 1, f"{len(built)} default pools were constructed; the loser's threads leak"


_ASYNC_ONLY_MESSAGE = (
    r"^AsyncGetUserAuth\.get_user is async, so sync access to request\.user cannot run it\. "
    r"Use `await request\.auser\(\)`, or define get_user_sync on the backend\.$"
)


def test_object_without_get_user_has_no_user_loader():
    assert resolve_user_loader(object()) is None


def test_async_only_get_user_rejects_sync_access(fresh_orm_executor):
    """A backend with an async ``get_user`` and no ``get_user_sync`` has no sync loader.

    Sync access to ``request.user`` cannot drive the coroutine without a
    second event loop on a pool thread. Bolt raises instead. The async loader
    of ``await request.auser()`` awaits ``get_user`` as a coroutine of the
    request and sends its query to the ORM pool.
    """
    fresh_orm_executor(workers=1)
    user = _make_user("async-only")

    backend = AsyncGetUserAuth(secret="user-loader-orm-pool-test-secret", algorithms=["HS256"])
    loaders = resolve_user_loader(backend)
    assert loaders is not None
    loader, aloader = loaders

    with pytest.raises(RuntimeError, match=_ASYNC_ONLY_MESSAGE):
        loader(str(user.pk), None)
    with pytest.raises(RuntimeError, match=_ASYNC_ONLY_MESSAGE):
        _force_on_event_loop(loader, str(user.pk), None)
    assert backend.orm_threads == []

    loaded = asyncio.run(aloader(str(user.pk), None))
    assert loaded is not None
    assert loaded.pk == user.pk
    assert backend.orm_threads[0].startswith(ORM_THREAD_PREFIX), (
        f"the database query ran on {backend.orm_threads[0]!r}; expected the bounded ORM pool."
    )


def test_inline_reentry_runs_in_a_context_copy(fresh_orm_executor):
    """The inline branch must scope ContextVar writes like the executor branch.

    The executor path runs the callable inside a copied context, so mutations
    stay invisible to the caller. The inline reentrant path must behave the
    same — otherwise the same handler observes different contextvar semantics
    depending on which thread forced the load.
    """
    fresh_orm_executor(workers=1)
    probe = contextvars.ContextVar("bolt_inline_probe", default="outer")

    def mutate():
        probe.set("mutated")
        return probe.get()

    def driver():
        assert concurrency.in_orm_executor_thread()
        assert concurrency.run_orm_blocking(mutate) == "mutated"
        return probe.get()

    seen_after = concurrency._get_orm_executor().submit(driver).result(timeout=5)
    assert seen_after == "outer", (
        f"caller observed {seen_after!r}; the inline reentrant path leaked a "
        "ContextVar mutation that the executor path would have scoped"
    )


@pytest.mark.parametrize("workers", ["not-a-number", "0"])
def test_invalid_orm_threads_value_stops_the_pool(fresh_orm_executor, workers):
    """A bad DJANGO_BOLT_ORM_THREADS value is a deployment error, not a default."""
    with pytest.raises(
        ImproperlyConfigured, match=rf"^DJANGO_BOLT_ORM_THREADS must be an int of 1 or more, got '{workers}'\.$"
    ):
        fresh_orm_executor(workers=workers)


class SyncGetUserAuth(APIKeyAuthentication):
    """Backend with a sync ``get_user``. The user is a tuple of what the backend received."""

    def get_user(self, user_id, auth_context):
        return ("sync", user_id, auth_context)


class CoroutineGetUserAuth(APIKeyAuthentication):
    """Backend with an async ``get_user``. The user is a tuple of what the backend received."""

    async def get_user(self, user_id, auth_context):
        return ("async", user_id, auth_context)


@pytest.fixture
def backend_registry(monkeypatch):
    """Give the test an empty registry of auth backends, and restore the real one after."""
    monkeypatch.setattr(user_loader, "_auth_backend_registry", {})
    monkeypatch.setattr(user_loader, "_resolved_loader_registry", {})


def test_lazy_user_passes_the_auth_context_to_the_backend():
    """A backend with its own ``get_user`` reads the claims of the request."""
    lazy = user_loader.LazyUser(resolve_user_loader(SyncGetUserAuth()), "7", {"role": "a"})

    assert tuple(lazy) == ("sync", "7", {"role": "a"})
    assert repr(lazy) == "<LazyUser: ('sync', '7', {'role': 'a'})>"


def test_registered_backend_is_found_by_name(backend_registry):
    backend = SyncGetUserAuth()
    user_loader.register_auth_backend("custom", backend)

    assert user_loader.get_registered_backend("custom") is backend
    assert user_loader.get_registered_backend("other") is None


def test_load_user_sync_calls_the_registered_backend(backend_registry, fresh_orm_executor):
    fresh_orm_executor()
    user_loader.register_auth_backend("custom", SyncGetUserAuth())

    assert user_loader.load_user_sync("7", "custom", {"role": "a"}) == ("sync", "7", {"role": "a"})
    # A request with no user ID has no user, also when the backend can load one.
    assert user_loader.load_user_sync(None, "custom", {"role": "a"}) is None
    assert user_loader.load_user_sync("", "custom", {"role": "a"}) is None


def test_load_user_sync_gives_none_for_a_backend_without_user_resolution(backend_registry):
    user_loader.register_auth_backend("api_key", APIKeyAuthentication(api_keys={"k"}))

    assert user_loader.load_user_sync("7", "api_key", {}) is None


def test_load_user_sync_loads_the_django_user_for_an_unregistered_backend(backend_registry, fresh_orm_executor):
    """Session auth has no backend instance. It loads the user from the Django user model."""
    fresh_orm_executor()
    user = _make_user("session-user")

    loaded = user_loader.load_user_sync(str(user.pk), "session", {})

    assert loaded is not None
    assert loaded.pk == user.pk
    assert user_loader.load_user_sync("999999", "session", {}) is None


def test_load_user_awaits_the_registered_backend(backend_registry, fresh_orm_executor):
    fresh_orm_executor()
    user_loader.register_auth_backend("async", CoroutineGetUserAuth())
    user_loader.register_auth_backend("sync", SyncGetUserAuth())

    assert asyncio.run(user_loader.load_user("7", "async", {"role": "a"})) == ("async", "7", {"role": "a"})
    assert asyncio.run(user_loader.load_user("7", "sync", {"role": "a"})) == ("sync", "7", {"role": "a"})
    # A request with no user ID has no user, also when the backend can load one.
    assert asyncio.run(user_loader.load_user(None, "async", {"role": "a"})) is None
    assert asyncio.run(user_loader.load_user("", "async", {"role": "a"})) is None


def test_load_user_gives_none_without_a_backend_that_resolves_users(backend_registry):
    user_loader.register_auth_backend("api_key", APIKeyAuthentication(api_keys={"k"}))

    assert asyncio.run(user_loader.load_user("7", "api_key", {})) is None
    assert asyncio.run(user_loader.load_user("7", "unregistered", {})) is None
    assert asyncio.run(user_loader.load_user("7", None, {})) is None


def test_default_async_loader_loads_the_django_user(fresh_orm_executor):
    fresh_orm_executor()
    user = _make_user("async-default")

    loaded = asyncio.run(user_loader.default_django_user_aloader(str(user.pk), None))

    assert loaded is not None
    assert loaded.pk == user.pk
    assert asyncio.run(user_loader.default_django_user_aloader("999999", None)) is None
