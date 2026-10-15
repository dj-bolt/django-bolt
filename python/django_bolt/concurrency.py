"""Thread pool execution utilities for sync handlers.

This module provides utilities to run synchronous callables in a thread pool,
enabling concurrent execution of I/O-bound sync handlers without blocking
the async event loop.

Inspired by Litestar's concurrency module.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
import os
import sys
import threading
import time
from asyncio.events import _get_running_loop, _set_running_loop
from collections.abc import Callable, Coroutine
from functools import partial

from asgiref.sync import AsyncToSync, SyncToAsync, ThreadSensitiveContext, sync_to_async
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.backends.signals import connection_created

logger = logging.getLogger(__name__)

__all__ = ("LaneAffinityError", "in_orm_executor_thread", "run_in_orm_executor", "run_orm_blocking", "sync_to_thread")


# Bolt keeps Django connections open across requests and does not run
# ``close_old_connections`` on every request. A connection that died would
# stay on its thread forever. The pool hand-offs below drop unusable
# connections on the error path only (zero cost when the call succeeds).
# When CONN_MAX_AGE or CONN_HEALTH_CHECKS is set, the same check also runs
# before each call so Django can age out and health-check connections.
_check_before_call = False

# The aliases whose DATABASES entry sets OPTIONS["pool"]. A pooled connection
# goes back to its pool when the thread that holds it has no more work: a
# worker of a Bolt executor when its queue is empty, a lane at the end of its
# request, and another thread at the end of its outermost guarded call. A
# thread that kept it would hold a pool slot while it is idle, and idle threads
# and lanes can outnumber max_size. The pool checks its own connections, so the
# check before a call leaves these aliases out.
_pooled_aliases: frozenset[str] = frozenset()


def configure_connection_checks() -> None:
    """Read the database settings: the pooled aliases, and whether the pre-call check runs."""
    global _check_before_call, _pooled_aliases
    from django.conf import settings  # noqa: PLC0415 — needs configured settings, resolved lazily

    if not settings.configured:
        return
    pooled = set()
    check_before_call = False
    for alias, db in connections.settings.items():
        if db.get("OPTIONS", {}).get("pool"):
            pooled.add(alias)
        elif db["CONN_MAX_AGE"] or db["CONN_HEALTH_CHECKS"]:
            check_before_call = True
    _pooled_aliases = frozenset(pooled)
    _check_before_call = check_before_call


def _drop_broken_connections() -> None:
    """Close the calling thread's connections that Django reports as unusable or obsolete.

    A connection inside an open ``atomic`` block is left alone: its autocommit
    state is off by design, and the block restores it on exit. Django's test
    client skips ``close_old_connections`` for the same reason.
    """
    dead = []
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            raw = _close_if_unusable_or_obsolete(conn)
            if raw is not None:
                dead.append((conn, raw))
    if dead:
        _connections_died(dead)


def _close_if_unusable_or_obsolete(conn) -> object | None:
    """Run ``conn.close_if_unusable_or_obsolete()``.

    Returns:
        The driver connection that it closed because it does not answer, or None.
    """
    raw = conn.connection
    # Django closes a connection with the wrong autocommit before it tests the
    # connection, so errors_occurred then says nothing about the connection.
    tested = raw is not None and conn.errors_occurred and conn.get_autocommit() == conn.settings_dict["AUTOCOMMIT"]
    conn.close_if_unusable_or_obsolete()
    # Django clears errors_occurred when the connection still answers.
    return raw if tested and conn.errors_occurred else None


# A database restart or failover kills every connection to that database at
# once, but each thread sees this only when it uses its own. The thread that
# finds the first dead connection advances the generation of its alias. Each
# connection carries the generation it opened in, and a thread closes its older
# connections of that alias before its next call, so it reconnects instead of
# failing a request of its own. Only a connection that opened in the current
# generation advances it: the other threads that meet a dead connection of the
# same event find an older one, so one event makes one reconnect wave.
#
# A pooled alias needs no wave, because its threads hold no connection between
# units of work. The thread that finds a dead pooled connection drains the pool
# instead (see ``_drain_pool``).
#
# ``_generation_epoch`` counts the advances of every alias. Each thread records
# the last epoch that it checked, so the check before a call costs one
# comparison until the next connection dies.
_generations: dict[str, int] = {}
_generation_epoch = 0
_generation_lock = threading.Lock()
_GENERATION_ATTR = "_django_bolt_connection_generation"
_POOL_EPOCH_ATTR = "_django_bolt_pool_epoch"
# alias -> number of drains of its pool. A driver connection records the value
# at its first checkout.
_pool_epochs: dict[str, int] = {}


class _ThreadState(threading.local):
    # Class defaults: a missing attribute of a thread-local costs an exception on each read.
    epoch = 0
    # The nesting depth of _call_guarded on this thread. Pooled connections go
    # back to their pools when the outermost call ends.
    depth = 0
    # Set on a request lane. A lane runs its checks and gives back its pooled
    # connections one time, at the start and the end of its request.
    is_lane = False
    active = False
    # Set when this thread checks out a pooled connection. A call that used no
    # pooled connection skips the release, which reads Django's connection
    # handler through a lock.
    holds_pooled = False
    # Set on a worker of a Bolt executor. It gives its pooled connections back
    # when its queue is empty (see _ReleasePooledConnectionsWhenIdle), not
    # after each call, so calls that wait in its queue use the same connection.
    releases_when_idle = False


_thread_state = _ThreadState()


def _stamp_connection_generation(sender: object, connection: object, **kwargs: object) -> None:
    alias = connection.alias
    setattr(connection, _GENERATION_ATTR, _generations.get(alias, 0))
    if alias in _pooled_aliases:
        # Django sends connection_created for each checkout from a pool, so the
        # epoch goes on the driver connection, which keeps it while it is in the pool.
        raw = connection.connection
        if not hasattr(raw, _POOL_EPOCH_ATTR):
            setattr(raw, _POOL_EPOCH_ATTR, _pool_epochs.get(alias, 0))
        _thread_state.holds_pooled = True


connection_created.connect(_stamp_connection_generation, dispatch_uid="django_bolt.connection_generation")


def _connections_died(dead: list[tuple[object, object]]) -> None:
    """Start a reconnect wave or drain a pool for each event that ``dead`` reports.

    ``dead`` holds (Django connection, driver connection) pairs.
    """
    global _generation_epoch
    for conn, raw in dead:
        alias = conn.alias
        if alias in _pooled_aliases:
            _drain_pool(conn, raw)
            continue
        with _generation_lock:
            generation = _generations.get(alias, 0)
            if getattr(conn, _GENERATION_ATTR, -1) == generation:
                _generations[alias] = generation + 1
                _generation_epoch += 1


def _drain_pool(conn: object, raw: object) -> None:
    """Drain the pool of ``conn`` when ``raw`` is the first of its connections that the event ended.

    The event that ended this connection usually ended the other connections
    of its pool too. Without a check callback, the pool would give each dead
    idle connection to a request that then fails. With one, getconn() tests
    idle connections one at a time and sleeps after each one that fails: 0, 1,
    2, 4 s and so on. drain() closes the idle connections with no network I/O,
    so a partition cannot make this thread wait. The pool also drops each older
    connection that a thread gives back. A driver connection from an earlier
    drain is part of an event that a drain already handled, so it drains nothing.
    """
    pool = getattr(conn, "pool", None)
    # psycopg_pool adds drain() in 3.3. An older pool keeps its idle
    # connections; each fails one request (see check_pool_environment).
    if pool is None or not hasattr(pool, "drain"):
        return
    alias = conn.alias
    with _generation_lock:
        epoch = _pool_epochs.get(alias, 0)
        if getattr(raw, _POOL_EPOCH_ATTR, epoch) != epoch:
            return
        _pool_epochs[alias] = epoch + 1
    pool.drain()


def _close_connections_of_older_generations() -> None:
    """Close the calling thread's connections that opened before their alias last advanced."""
    epoch = _generation_epoch
    if _thread_state.epoch == epoch:
        return
    closed_all = True
    for conn in connections.all(initialized_only=True):
        if conn.connection is None or getattr(conn, _GENERATION_ATTR, -1) >= _generations.get(conn.alias, 0):
            continue
        if conn.in_atomic_block:
            # The block owns the connection. The next call after the block closes it.
            closed_all = False
        # The TestClient shares one connection between threads. Its owner closes it.
        elif not getattr(conn, _SHARED_TEST_CONNECTION_ATTR, False):
            conn.close()
    if closed_all:
        _thread_state.epoch = epoch


def _check_before_pool_call() -> None:
    stale = _thread_state.epoch != _generation_epoch
    # A lane does these checks one time, at the start of its request.
    if (stale or _check_before_call) and not _thread_state.is_lane:
        if stale:
            _close_connections_of_older_generations()
        if _check_before_call:
            _drop_broken_connections()


def release_pooled_connections() -> None:
    """Give the calling thread's pooled connections back to their pools.

    A connection inside an open ``atomic`` block stays: the block owns it.
    """
    kept = False
    for alias in _pooled_aliases:
        conn = connections[alias]
        if conn.connection is None or getattr(conn, _SHARED_TEST_CONNECTION_ATTR, False):
            continue
        if conn.in_atomic_block:
            kept = True
        else:
            conn.close()
    _thread_state.holds_pooled = kept


def _call_guarded[T](fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Call ``fn`` on the pool thread and drop dead connections when it raises."""
    if (_generation_epoch and _thread_state.epoch != _generation_epoch) or _check_before_call:
        _check_before_pool_call()
    if _pooled_aliases:
        return _call_guarded_pooled(fn, args, kwargs)
    try:
        return fn(*args, **kwargs)
    except Exception:
        _drop_broken_connections()
        raise


def _call_guarded_pooled[T](fn: Callable[..., T], args: tuple[object, ...], kwargs: dict[str, object]) -> T:
    """``_call_guarded`` when a database uses a pool: the outermost call also gives back its pooled connections."""
    state = _thread_state
    state.depth += 1
    try:
        return fn(*args, **kwargs)
    except Exception:
        _drop_broken_connections()
        raise
    finally:
        state.depth -= 1
        # A lane gives its pooled connections back at the end of its request,
        # and an executor worker when its queue is empty.
        if state.holds_pooled and not state.depth and not (state.is_lane or state.releases_when_idle):
            release_pooled_connections()


class _ReleasePooledConnectionsWhenIdle:
    """The idle semaphore of a Bolt executor. A worker that goes idle gives its pooled connections back.

    A ``ThreadPoolExecutor`` worker calls ``release()`` on its own thread when
    its queue is empty, just before it waits for the next item (CPython 3.12
    to 3.14). While calls wait in the queue, the worker keeps its connection,
    so a burst does not pay a pool checkout for each call. An idle worker
    holds no pool slot.
    """

    __slots__ = ("_semaphore",)

    def __init__(self, semaphore: threading.Semaphore) -> None:
        self._semaphore = semaphore

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        return self._semaphore.acquire(blocking, timeout)

    def release(self, n: int = 1) -> None:
        if _thread_state.holds_pooled:
            try:
                release_pooled_connections()
            except Exception:
                # An exception here would end the worker thread, and the
                # executor would not start a new one.
                logger.exception("Could not give the pooled database connections of an idle worker back")
        self._semaphore.release(n)


# False when this Python's ThreadPoolExecutor has no idle semaphore. The
# workers then give their pooled connections back after each call.
_idle_release_available = True


def _new_executor(
    workers: int, thread_name_prefix: str, initializer: Callable[[], None] | None = None
) -> concurrent.futures.ThreadPoolExecutor:
    """Make a Bolt executor whose workers give their pooled connections back when they go idle.

    ``initializer`` runs on each worker thread when it starts, as in ``ThreadPoolExecutor``.
    """
    global _idle_release_available
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=thread_name_prefix,
        initializer=partial(_mark_executor_thread, initializer),
    )
    semaphore = getattr(executor, "_idle_semaphore", None)
    if semaphore is None:
        _idle_release_available = False
    else:
        executor._idle_semaphore = _ReleasePooledConnectionsWhenIdle(semaphore)
    return executor


def _mark_executor_thread(initializer: Callable[[], None] | None) -> None:
    _thread_state.releases_when_idle = _idle_release_available
    if initializer is not None:
        initializer()


# Request lanes: see crates/bolt-core/src/lane.rs.
_SHARED_TEST_CONNECTION_ATTR = "_django_bolt_shared_test_connection"


def mark_lane_thread() -> None:
    """Mark the calling thread as a lane. Rust calls this when a lane starts."""
    _thread_state.is_lane = True
    # A lane checks its own connections, so it must not depend on the start of a pool.
    configure_connection_checks()


def _on_lane_thread() -> bool:
    return _thread_state.is_lane


def in_lane_mode() -> bool:
    """Whether the calling thread is a request lane that runs a complete request."""
    return _thread_state.active


def run_on_request_lane[T](coro_fn: Callable[..., Coroutine[object, object, T]], *args: object) -> T:
    """Run a complete request on the calling lane thread.

    ``coro_fn(*args)`` must not suspend. In lane mode, each thread hop in the
    dispatch path runs inline, so the coroutine ends in one step.
    """
    open_lane_request()
    # A lane serves many requests. A new context keeps the context variables
    # of one request away from the next one, as a new asyncio task does.
    return contextvars.Context().run(_drive_in_lane_mode, coro_fn, args)


def _drive_in_lane_mode[T](coro_fn: Callable[..., Coroutine[object, object, T]], args: tuple[object, ...]) -> T:
    # Lane mode ends with the request. The next owner of this lane can be an
    # async request, whose coroutines do suspend.
    _thread_state.active = True
    # ``async_to_sync`` in a sync handler runs its coroutine on a different
    # thread. With this context, its thread-sensitive work returns to the lane.
    SyncToAsync.thread_sensitive_context.set(ThreadSensitiveContext())
    try:
        return drive_on_lane(coro_fn(*args))
    finally:
        _thread_state.active = False
        close_lane_request()


def drive_on_lane[T](coro: Coroutine[object, object, T]) -> T:
    """Run a coroutine that does not suspend. On a lane, each thread hop runs inline."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise RuntimeError(
        "A coroutine suspended on a request lane. Lane dispatch supports request flows with no await only."
    )


def open_lane_request() -> None:
    """Start a request on a lane. Rust calls this before the first sync call of an async request."""
    _close_connections_of_older_generations()
    if _check_before_call:
        _drop_broken_connections()


def close_lane_request() -> None:
    """End a request on a lane. For an async request, Rust calls this when the lane comes back.

    Bolt does not see a database error that user code handles, or one from a
    direct ``sync_to_async`` call. Django does this check on ``request_finished``.
    """
    dead = []
    for conn in connections.all(initialized_only=True):
        if conn.errors_occurred and not conn.in_atomic_block:
            raw = _close_if_unusable_or_obsolete(conn)
            if raw is not None:
                dead.append((conn, raw))
    if dead:
        _connections_died(dead)
    if _thread_state.holds_pooled:
        release_pooled_connections()


def _close_thread_connections() -> None:
    """Close the calling thread's connections. A pooled connection goes back to its pool."""
    for conn in connections.all(initialized_only=True):
        # The TestClient shares one connection between threads. Its owner closes it.
        if not getattr(conn, _SHARED_TEST_CONNECTION_ATTR, False):
            conn.close()


def close_lane_connections() -> None:
    """Close the connections of a lane that stops. Rust calls this on the lane thread."""
    _close_thread_connections()


def close_stream_connections() -> None:
    """Close the connections of a thread that iterated a sync streaming body. Rust calls this on that thread.

    The thread exists for one response. A connection that it opened would
    otherwise stay open until garbage collection, and a pooled one would never
    go back to its pool: each such response would hold one pool slot for the
    life of the process.
    """
    _close_thread_connections()


# Shared default pool for generic blocking work. Passing an explicit executor
# keeps the compatibility asyncio loop and WorkerLoop from each lazily creating
# a separate default pool. Thread count is bounded and independently tunable
# from the database-aware ORM pool below.
_default_executor: concurrent.futures.ThreadPoolExecutor | None = None

# Guards lazy construction, like the ORM pool's lock below: two racing callers
# would each build a pool and the loser's threads would leak.
_default_executor_lock = threading.Lock()


def _thread_count(name: str) -> int | None:
    """
    Read a thread count from the environment variable `name`.

    Returns:
        The count, or None when the variable is not set or is empty.

    Raises:
        ImproperlyConfigured: The value is not an int of 1 or more.
    """
    raw = os.environ.get(name, "")
    if not raw.strip():
        return None
    try:
        count = int(raw)
    except ValueError:
        count = 0
    if count < 1:
        raise ImproperlyConfigured(f"{name} must be an int of 1 or more, got {raw!r}.")
    return count


def check_pool_environment() -> None:
    """
    Check the thread counts of the pools and the connection pools at startup.

    The pools start at their first use. The server calls this function at
    startup, so an invalid value stops startup and not a later request.

    Raises:
        ImproperlyConfigured: DJANGO_BOLT_EXECUTOR_THREADS or
            DJANGO_BOLT_ORM_THREADS is not an int of 1 or more.
    """
    _thread_count("DJANGO_BOLT_EXECUTOR_THREADS")
    _thread_count("DJANGO_BOLT_ORM_THREADS")
    configure_connection_checks()
    _check_connection_pools()


def _check_connection_pools() -> None:
    """Log a warning for each PostgreSQL connection pool that Bolt cannot fully support.

    Bolt drains the pool of a connection that it finds dead. ``drain()`` is in
    psycopg_pool 3.3 and later. Without it, the pool keeps its dead idle
    connections after a restart, and each one fails one request.

    A Bolt worker gives its pooled connection back when it goes idle, so a
    pool ``check`` callback runs on each checkout after an idle time: one more
    round trip for many ORM calls. ``CONN_HEALTH_CHECKS`` adds that callback.
    The drain already removes the connections that a restart ends.
    """
    if not settings.configured:
        return
    for alias, db in connections.settings.items():
        pool_options = db.get("OPTIONS", {}).get("pool")
        # The vendor covers backends on top of PostgreSQL, such as PostGIS.
        if not pool_options or connections[alias].vendor != "postgresql":
            continue
        # The `pool` extra installs psycopg_pool. Only a pooled PostgreSQL database needs it.
        from psycopg_pool import ConnectionPool  # noqa: PLC0415

        if not hasattr(ConnectionPool, "drain"):
            logger.warning(
                "DATABASES[%r] uses Django's connection pool with a psycopg_pool before 3.3. "
                "After a database restart, each dead idle connection of the pool fails one request. "
                'Install "django-bolt[pool]" for psycopg_pool 3.3 or later.',
                alias,
            )
        has_check = isinstance(pool_options, dict) and pool_options.get("check") is not None
        if has_check or db.get("CONN_HEALTH_CHECKS"):
            logger.warning(
                "DATABASES[%r] gives its connection pool a check callback%s. Bolt returns a pooled "
                "connection when a worker goes idle, so the check adds a round trip to many calls. "
                "Bolt drains the pool when it finds a dead connection, so the check is not necessary.",
                alias,
                "" if has_check else " through CONN_HEALTH_CHECKS",
            )


def close_connections_and_pools() -> None:
    """Close the calling thread's connections and each connection pool of the process.

    A forked child inherits the sockets of the open connections of its parent.
    Two processes that use one socket read each other's query results. A pool
    keeps its idle connections open after ``close()``, so the pool must close
    too. ``runbolt`` calls this in its parent before each fork.
    """
    for conn in connections.all(initialized_only=True):
        conn.close()
    for alias, db in connections.settings.items():
        if not db.get("OPTIONS", {}).get("pool"):
            continue
        close_pool = getattr(connections[alias], "close_pool", None)
        if close_pool is not None:
            close_pool()


def _get_default_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _default_executor
    executor = _default_executor
    if executor is not None:
        return executor
    with _default_executor_lock:
        if _default_executor is None:
            workers = _thread_count("DJANGO_BOLT_EXECUTOR_THREADS") or min(32, (os.cpu_count() or 1) + 4)
            configure_connection_checks()
            _default_executor = _new_executor(workers, "bolt_default")
        return _default_executor


# Bounded executor for framework-initiated QuerySet evaluation (async handlers
# returning QuerySets, pagination counts/slices). Parallel — unlike asgiref's
# thread_sensitive single thread — but CAPPED: each executor thread holds its
# own long-lived DB connection, and unbounded parallelism actively hurts
# SQLite (measured: /users/full10 at C=32 was ~2x faster through one shared
# connection than through the unbounded default pool). A small cap keeps the
# C=1 parallelism win while bounding connection count and lock contention.
# Tune with DJANGO_BOLT_ORM_THREADS (default 4).
_orm_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _default_orm_workers() -> int:
    """Vendor-aware default for the ORM pool size.

    Measured on the example project (/users/full10, C=32, single process):
    SQLite throughput scales INVERSELY with connection count — 1 thread beat
    4 threads by ~80% and the unbounded pool by ~2.5x (file-lock contention +
    per-connection page caches). Networked databases (Postgres/MySQL) benefit
    from parallel connections instead. Override with DJANGO_BOLT_ORM_THREADS.
    """
    try:
        if all_databases_sqlite():
            return 1
    except Exception as exc:  # unconfigured settings etc. — fall back to the parallel default
        logger.debug("ORM executor vendor detection failed, using default pool size: %s", exc)
    return 4


def all_databases_sqlite() -> bool:
    """Whether every configured database is SQLite, a local database with fast queries."""
    engines = {db.get("ENGINE", "") for db in settings.DATABASES.values()}
    return bool(engines) and all("sqlite" in engine for engine in engines)


# Set on every ORM pool thread so callers already running inside the pool can
# detect it. Blocking on the pool from one of its own threads would wait on a
# slot the caller is itself holding — with the SQLite default of one thread,
# that never resolves.
_orm_thread_state = threading.local()


def _mark_orm_thread() -> None:
    _orm_thread_state.in_pool = True


def in_orm_executor_thread() -> bool:
    """Whether the calling thread is an ORM pool worker."""
    return getattr(_orm_thread_state, "in_pool", False)


# Guards lazy construction. Check-then-create was safe while the only caller
# ran on the event loop thread; user loading forces `request.user` from
# arbitrary threads, where two racing callers would each build a pool and the
# loser would leak its threads and their database connections — precisely the
# accounting this pool exists to enforce.
_orm_executor_lock = threading.Lock()


def _get_orm_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _orm_executor
    executor = _orm_executor
    if executor is not None:
        return executor
    with _orm_executor_lock:
        if _orm_executor is None:
            workers = _thread_count("DJANGO_BOLT_ORM_THREADS") or _default_orm_workers()
            configure_connection_checks()
            _orm_executor = _new_executor(workers, "bolt_orm", _mark_orm_thread)
        return _orm_executor


# True in the context of each call that the ORM pool runs. ``async_to_sync``
# on a worker starts its loop on a new thread, and it carries this value there.
_holds_orm_slot: contextvars.ContextVar[bool] = contextvars.ContextVar("django_bolt_holds_orm_slot", default=False)


def _call_in_orm_slot[T](fn: Callable[..., T], *args: object) -> T:
    """Call ``fn`` on an ORM pool worker and mark the context as the holder of the slot."""
    _holds_orm_slot.set(True)
    return _call_guarded(fn, *args)


def _slot_is_held() -> bool:
    """Whether the calling context holds an ORM pool slot: an ORM pool thread, or a context from one."""
    return in_orm_executor_thread() or _holds_orm_slot.get()


def _pool_for(slot_is_held: bool) -> tuple[concurrent.futures.ThreadPoolExecutor, Callable[..., object]]:
    """The pool and the call wrapper for a hand-off.

    A hand-off from a context that holds a slot cannot use the ORM pool: it
    waits on the slot that its own thread holds, and a one-thread pool never
    frees it. That rare case uses the default pool and one connection outside
    the ORM budget.
    """
    if slot_is_held:
        return _get_default_executor(), _call_guarded
    return _get_orm_executor(), _call_in_orm_slot


def _orm_handoff() -> tuple[concurrent.futures.ThreadPoolExecutor, Callable[..., object]]:
    """The pool and the call wrapper for a framework hand-off from a coroutine.

    The coroutine can run inside a slot. An ORM pool worker runs a loop on
    its own thread in ``asyncio.run``, or on a new thread in
    ``async_to_sync``; the slot marker of this module reaches that loop. A
    thread-sensitive call through :class:`_ThreadSensitiveExecutor` holds a
    slot too, but asgiref runs the sync function in a context that it copied
    before the hand-off, so the marker of this module is not set there.
    asgiref sets its own marker, ``deadlock_context``, before the copy, and
    an ``async_to_sync`` inside the function carries it to the coroutine.
    """
    return _pool_for(_slot_is_held() or SyncToAsync.deadlock_context.get(False))


class _ThreadSensitiveExecutor:
    """The asgiref executor for thread-sensitive calls of a request with no thread-sensitive context.

    Django's async ORM API (``aget``, ``acount``, ``async for``, ``auser``) is
    ``sync_to_async(thread_sensitive=True)``. For a route without Django
    middleware, asgiref sends these calls to its class-level executor. That
    default is one thread per process: one query in flight, whatever the
    worker count. No Bolt code runs on that thread, so a dead connection on
    it is never dropped.

    This executor sends the call to the ORM pool. The pool is bounded by
    ``DJANGO_BOLT_ORM_THREADS``, and the call runs through ``_call_guarded``,
    which drops a broken connection on the error path. A route with Django
    middleware has a thread-sensitive context, and asgiref never reaches this
    executor for it.
    """

    __slots__ = ()

    def submit(self, fn: Callable[..., object], *args: object) -> concurrent.futures.Future:
        # asgiref sets ``deadlock_context`` before each call that reaches this
        # executor, so only the slot markers of this module decide here.
        executor, call = _pool_for(_slot_is_held())
        return executor.submit(contextvars.copy_context().run, call, fn, *args)


# Installed once at import. asgiref reads the attribute on each thread-sensitive call.
SyncToAsync.single_thread_executor = _ThreadSensitiveExecutor()


# Bound once: ``run_orm_blocking`` reads it on each forced ``request.user``.
_thread_sensitive_context = SyncToAsync.thread_sensitive_context


class LaneAffinityError(RuntimeError):
    """A sync ORM call ran away from the lane of a request with Django middleware.

    A thread with no event loop that is not the lane raises it, for example
    a thread from ``asyncio.to_thread``. That thread cannot wait for the
    lane, because the lane can wait for it. A request whose lane is gone,
    for example a task that runs after the response, raises it too. The
    dispatch of the request adds the route and the handler to the message.
    """


_OFF_LANE_ORM_CALL = (
    "A sync ORM call, such as a read of request.user, ran on a thread that cannot reach the lane of "
    "a request with Django middleware. The query must run on the lane, which keeps the thread-local "
    "state of the request. Read request.user on the thread of the request, or use "
    "`await request.auser()` in async code."
)


def run_orm_blocking[T](fn: Callable[..., T], *args: object) -> T:
    """Blocking counterpart of :func:`run_in_orm_executor` for callers that cannot await.

    Exists for the synchronous user-loading path: ``request.user`` is a
    ``SimpleLazyObject`` forced from code that cannot await. The query runs
    with the caller's contextvars visible, so a request-scoped database
    router applies to user loading as it does to QuerySet evaluation.

    A thread with no running event loop runs ``fn`` inline. This keeps a
    request lane on its own connection and thread-local state. A thread with
    a running loop also runs ``fn`` inline: the read blocks the loop in any
    case, and a hop to the ORM pool only adds two thread wakeups. Django's
    ``async_unsafe`` guard sees no running loop during the call.

    A request with Django middleware keeps its thread-local state on its
    lane, for example a tenant schema. A query on another thread does not
    see that state. Thus on the loop of such a request, ``fn`` runs on the
    lane, and the loop waits for it. The lane can itself wait for the loop
    in ``async_to_sync``. It then runs work only through its asgiref
    ``CurrentThreadExecutor``, so ``fn`` goes there, as asgiref sends
    thread-sensitive work. A thread with no loop that is not the lane gets
    :class:`LaneAffinityError`: the lane can wait for that thread.
    ``await request.auser()`` does not block the loop.

    Both branches run ``fn`` in a copied context, so ContextVar writes stay
    scoped the same way. While the loop waits, asgiref must not see it as the
    parent loop of the thread that runs ``fn``: an ``async_to_sync`` in ``fn``
    would schedule its coroutine on the waiting loop and never return.
    """
    loop = _get_running_loop()
    if loop is None:
        if _thread_sensitive_context.get(None) is None or _thread_state.is_lane:
            return contextvars.copy_context().run(fn, *args)
        raise LaneAffinityError(_OFF_LANE_ORM_CALL)
    context = _thread_sensitive_context.get(None)
    if context is None:
        # The loop blocks on this read. Run it here: no hop, no second thread.
        # Nothing on this thread observes the running-loop flag during a
        # blocking call, so Django's async_unsafe guard lets the query through.
        _set_running_loop(None)
        started = time.perf_counter()
        try:
            return contextvars.copy_context().run(_call_without_parent_loop, fn, *args)
        finally:
            _set_running_loop(loop)
            waited = time.perf_counter() - started
            if waited >= _SLOW_LOOP_WAIT_SECONDS:
                _report_slow_loop_wait(waited)
    # The loop waits for the lane of the request. Pick the executor that
    # asgiref picks for thread-sensitive work: a lane that waits for the
    # loop in async_to_sync runs work only through its CurrentThreadExecutor.
    executor = getattr(AsyncToSync.executors, "current", None) or SyncToAsync.context_to_thread_executor.get(context)
    if executor is None:
        raise LaneAffinityError(_OFF_LANE_ORM_CALL)
    future = executor.submit(contextvars.copy_context().run, _call_without_parent_loop, fn, *args)
    started = time.perf_counter()
    result = future.result()
    waited = time.perf_counter() - started
    if waited >= _SLOW_LOOP_WAIT_SECONDS:
        _report_slow_loop_wait(waited)
    return result


def _call_without_parent_loop[T](fn: Callable[..., T], *args: object) -> T:
    """Call ``fn`` with no asgiref parent loop on this thread.

    :func:`run_orm_blocking` blocks the loop that asgiref recorded as the parent
    loop of a lane. An ``async_to_sync`` in ``fn`` then runs its own loop on a
    new thread, as it does on a thread that no loop started.
    """
    threadlocal = SyncToAsync.threadlocal
    parent_loop = getattr(threadlocal, "main_event_loop", None)
    parent_pid = getattr(threadlocal, "main_event_loop_pid", None)
    threadlocal.main_event_loop = None
    threadlocal.main_event_loop_pid = None
    try:
        return _call_guarded(fn, *args)
    finally:
        threadlocal.main_event_loop = parent_loop
        threadlocal.main_event_loop_pid = parent_pid


# A sync read of request.user that blocks the event loop this long is reported in dev mode.
_SLOW_LOOP_WAIT_SECONDS = 0.05
# (file, line) of each read that was reported. Each is reported one time.
_reported_slow_reads: set[tuple[str, int]] = set()
_FRAMEWORK_PATH_PARTS = tuple(f"{os.sep}{name}{os.sep}" for name in ("django_bolt", "django", "asgiref"))


def _report_slow_loop_wait(waited: float) -> None:
    """In dev mode, log the line of a sync read of request.user that blocked the event loop.

    The read can be in a helper, a template, or a serializer, where Bolt cannot
    load the user before the handler. The line is the first caller outside
    Bolt, Django, and asgiref. This runs only after a slow wait.
    """
    if not (settings.DEBUG or os.environ.get("DJANGO_BOLT_DEV_WORKER") == "1"):
        return
    frame = sys._getframe(2)
    while frame is not None and any(part in frame.f_code.co_filename for part in _FRAMEWORK_PATH_PARTS):
        frame = frame.f_back
    where = (frame.f_code.co_filename, frame.f_lineno) if frame is not None else ("<unknown>", 0)
    if where in _reported_slow_reads:
        return
    _reported_slow_reads.add(where)
    logger.warning(
        "A sync read of request.user blocked the event loop for %.0f ms at %s:%d. "
        "Use `await request.auser()` or a `CurrentUser` parameter. Bolt logs this one time for each line.",
        waited * 1000,
        *where,
    )


async def run_in_orm_executor[**P, T](fn: Callable[P, T], *args: P.args) -> T:
    """Run a framework-initiated ORM evaluation in the bounded ORM pool.

    The caller's contextvars context is carried into the pool thread —
    request-scoped state (e.g. a tenant-aware database router) must be
    visible while the QuerySet evaluates, exactly as it was under
    ``sync_to_async``.
    """
    # A Django middleware stack owns thread-local request state, such as a
    # tenant-selected database schema. Its ThreadSensitiveContext requires
    # every ORM call to use the request's dedicated worker.
    if in_lane_mode():
        return _call_guarded(fn, *args)
    if SyncToAsync.thread_sensitive_context.get(None) is not None:
        return await sync_to_async(_call_guarded, thread_sensitive=True)(fn, *args)

    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    executor, call = _orm_handoff()
    return await loop.run_in_executor(executor, ctx.run, call, fn, *args)


async def sync_to_thread[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run the synchronous callable ``fn`` asynchronously in a worker thread.

    This function uses :meth:`asyncio.loop.run_in_executor` to run the callable
    in the default thread pool executor. Context variables are preserved across
    the thread boundary.

    This is critical for sync handlers that perform I/O operations (like Django ORM
    queries) - it allows the async worker to handle other requests while the sync
    handler waits for I/O completion.

    Args:
        fn: Synchronous callable to execute
        *args: Positional arguments for the callable
        **kwargs: Keyword arguments for the callable

    Returns:
        The return value of the callable

    Example:
        >>> async def handle_request():
        ...     # Run blocking Django ORM query in thread pool
        ...     users = await sync_to_thread(User.objects.all)
        ...     return users

    Performance:
        - Adds ~50-100μs overhead per call
        - Enables concurrent I/O across multiple sync handlers
        - Expected 40-60% RPS improvement for I/O-bound sync handlers
    """
    # Run in default executor (thread pool)
    # Use Bolt's explicit shared executor so every supported loop uses one
    # bounded pool rather than creating a separate implicit default pool.
    # The caller's contextvars context is carried into the thread via ctx.run.
    # run_in_executor only forwards positional args — keyword args (e.g. Rust
    # prebound keyword-bound params) must be bound via partial.
    # A Django middleware stack owns thread-local request state. Use the
    # request's dedicated worker, as run_in_orm_executor does.
    if in_lane_mode():
        return _call_guarded(fn, *args, **kwargs)
    if SyncToAsync.thread_sensitive_context.get(None) is not None:
        return await sync_to_async(_call_guarded, thread_sensitive=True)(fn, *args, **kwargs)

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    if kwargs:
        return await loop.run_in_executor(_get_default_executor(), partial(ctx.run, _call_guarded, fn, *args, **kwargs))
    return await loop.run_in_executor(_get_default_executor(), ctx.run, _call_guarded, fn, *args)
