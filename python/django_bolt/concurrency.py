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
import threading
from asyncio.events import _get_running_loop
from collections.abc import Callable, Coroutine
from functools import partial

from asgiref.sync import SyncToAsync, ThreadSensitiveContext, sync_to_async
from django.db import connections

logger = logging.getLogger(__name__)

__all__ = ("in_orm_executor_thread", "run_in_orm_executor", "run_orm_blocking", "sync_to_thread")


# Bolt keeps Django connections open across requests and does not run
# ``close_old_connections`` on every request. A connection that died would
# stay on its thread forever. The pool hand-offs below drop unusable
# connections on the error path only (zero cost when the call succeeds).
# When CONN_MAX_AGE or CONN_HEALTH_CHECKS is set, the same check also runs
# before each call so Django can age out and health-check connections.
_check_before_call = False


def configure_connection_checks() -> None:
    """Enable the pre-call connection check when the database settings ask for it."""
    global _check_before_call
    from django.conf import settings  # noqa: PLC0415 — needs configured settings, resolved lazily

    if not settings.configured:
        return
    for db in connections.settings.values():
        if db["CONN_MAX_AGE"] or db["CONN_HEALTH_CHECKS"]:
            _check_before_call = True
            return


def _drop_broken_connections() -> None:
    """Close the calling thread's connections that Django reports as unusable or obsolete.

    A connection inside an open ``atomic`` block is left alone: its autocommit
    state is off by design, and the block restores it on exit. Django's test
    client skips ``close_old_connections`` for the same reason.
    """
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close_if_unusable_or_obsolete()


def _call_guarded[T](fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Call ``fn`` on the pool thread and drop dead connections when it raises."""
    # A lane does this check one time, at the start of its request.
    if _check_before_call and not _on_lane_thread():
        _drop_broken_connections()
    try:
        return fn(*args, **kwargs)
    except Exception:
        _drop_broken_connections()
        raise


# Request lanes: see crates/bolt-core/src/lane.rs.
_lane_state = threading.local()
_SHARED_TEST_CONNECTION_ATTR = "_django_bolt_shared_test_connection"


def mark_lane_thread() -> None:
    """Mark the calling thread as a lane. Rust calls this when a lane starts."""
    _lane_state.is_lane = True
    # A lane checks its own connections, so it must not depend on the start of a pool.
    configure_connection_checks()


def _on_lane_thread() -> bool:
    return getattr(_lane_state, "is_lane", False)


def in_lane_mode() -> bool:
    """Whether the calling thread is a request lane that runs a complete request."""
    return getattr(_lane_state, "active", False)


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
    _lane_state.active = True
    # ``async_to_sync`` in a sync handler runs its coroutine on a different
    # thread. With this context, its thread-sensitive work returns to the lane.
    SyncToAsync.thread_sensitive_context.set(ThreadSensitiveContext())
    try:
        return drive_on_lane(coro_fn(*args))
    finally:
        _lane_state.active = False
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
    if _check_before_call:
        _drop_broken_connections()


def close_lane_request() -> None:
    """End a request on a lane. For an async request, Rust calls this when the lane comes back.

    Bolt does not see a database error that user code handles, or one from a
    direct ``sync_to_async`` call. Django does this check on ``request_finished``.
    """
    for conn in connections.all(initialized_only=True):
        if conn.errors_occurred and not conn.in_atomic_block:
            conn.close_if_unusable_or_obsolete()


def close_lane_connections() -> None:
    """Close the connections of a lane that stops. Rust calls this on the lane thread."""
    for conn in connections.all(initialized_only=True):
        # The TestClient shares one connection between threads. Its owner closes it.
        if not getattr(conn, _SHARED_TEST_CONNECTION_ATTR, False):
            conn.close()


# Shared default pool for generic blocking work. Passing an explicit executor
# keeps the compatibility asyncio loop and WorkerLoop from each lazily creating
# a separate default pool. Thread count is bounded and independently tunable
# from the database-aware ORM pool below.
_default_executor: concurrent.futures.ThreadPoolExecutor | None = None

# Guards lazy construction, like the ORM pool's lock below: two racing callers
# would each build a pool and the loser's threads would leak.
_default_executor_lock = threading.Lock()


def _get_default_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _default_executor
    executor = _default_executor
    if executor is not None:
        return executor
    with _default_executor_lock:
        if _default_executor is None:
            raw = os.environ.get("DJANGO_BOLT_EXECUTOR_THREADS")
            platform_default = min(32, (os.cpu_count() or 1) + 4)
            try:
                workers = int(raw) if raw else platform_default
            except ValueError:
                workers = platform_default
                logger.warning(
                    "Ignoring invalid DJANGO_BOLT_EXECUTOR_THREADS=%r; using the default of %d",
                    raw,
                    workers,
                )
            configure_connection_checks()
            _default_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, workers), thread_name_prefix="bolt_default"
            )
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
        from django.conf import settings  # noqa: PLC0415 — needs configured settings, resolved lazily

        engines = {db.get("ENGINE", "") for db in settings.DATABASES.values()}
        if engines and all("sqlite" in engine for engine in engines):
            return 1
    except Exception as exc:  # unconfigured settings etc. — fall back to the parallel default
        logger.debug("ORM executor vendor detection failed, using default pool size: %s", exc)
    return 4


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
            raw = os.environ.get("DJANGO_BOLT_ORM_THREADS")
            try:
                workers = int(raw) if raw else _default_orm_workers()
            except ValueError:
                workers = _default_orm_workers()
                logger.warning(
                    "Ignoring invalid DJANGO_BOLT_ORM_THREADS=%r; using the default of %d",
                    raw,
                    workers,
                )
            configure_connection_checks()
            _orm_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, workers),
                thread_name_prefix="bolt_orm",
                initializer=_mark_orm_thread,
            )
        return _orm_executor


# True in the context of each call that the ORM pool runs. ``async_to_sync``
# on a worker starts its loop on a new thread, and it carries this value there.
_holds_orm_slot: contextvars.ContextVar[bool] = contextvars.ContextVar("django_bolt_holds_orm_slot", default=False)


def _call_in_orm_slot[T](fn: Callable[..., T], *args: object) -> T:
    """Call ``fn`` on an ORM pool worker and mark the context as the holder of the slot."""
    _holds_orm_slot.set(True)
    return _call_guarded(fn, *args)


def _submit_blocking(
    executor: concurrent.futures.ThreadPoolExecutor,
    call: Callable[..., object],
    fn: Callable[..., object],
    *args: object,
) -> object:
    """Submit ``call(fn, *args)`` to ``executor`` with the caller's context and block on it."""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, call, fn, *args).result()


def _orm_handoff() -> tuple[concurrent.futures.ThreadPoolExecutor, Callable[..., object]]:
    """The pool and the call wrapper for an ORM hand-off from the calling context.

    An ORM pool worker can run an event loop, on its own thread in
    ``asyncio.run``, or on a new thread in ``async_to_sync``. A hand-off from
    that loop cannot use the ORM pool: it waits on the slot that the worker
    holds, and a one-thread pool never frees it. That rare case uses the
    default pool and one connection outside the ORM budget.
    """
    if in_orm_executor_thread() or _holds_orm_slot.get():
        return _get_default_executor(), _call_guarded
    return _get_orm_executor(), _call_in_orm_slot


# Bound once: ``run_orm_blocking`` reads it on each forced ``request.user``.
_thread_sensitive_context = SyncToAsync.thread_sensitive_context

_OFF_LANE_ORM_CALL = (
    "A sync ORM call, such as a read of request.user, ran away from the thread of a request with "
    "Django middleware. The query must run on the lane of the request, which keeps its thread-local "
    "state. In async code, use `await request.auser()`. Or read request.user in a sync handler."
)


def run_orm_blocking[T](fn: Callable[..., T], *args: object) -> T:
    """Blocking counterpart of :func:`run_in_orm_executor` for callers that cannot await.

    Exists for the synchronous user-loading path: ``request.user`` is a
    ``SimpleLazyObject`` forced from code that cannot await. The query runs
    with the caller's contextvars visible, so a request-scoped database
    router applies to user loading as it does to QuerySet evaluation.

    A thread with no running event loop runs ``fn`` inline. This keeps a
    request lane on its own connection and thread-local state. A thread with
    a running loop cannot run the ORM inline, so ``fn`` runs on the ORM pool,
    and the loop waits for it.

    A request with Django middleware keeps its thread-local state on its
    lane, for example a tenant schema. A query on another thread does not
    see that state, and a loop that waits for the lane can deadlock. Thus
    in such a request, a caller that is not on the lane gets ``RuntimeError``.
    That includes the event loop and a thread from ``asyncio.to_thread``.
    Code that can await uses ``await request.auser()`` instead.

    Both branches run ``fn`` in a copied context, so ContextVar writes stay
    scoped the same way.
    """
    if _get_running_loop() is None:
        if _thread_sensitive_context.get(None) is None or getattr(_lane_state, "is_lane", False):
            return contextvars.copy_context().run(fn, *args)
        raise RuntimeError(_OFF_LANE_ORM_CALL)
    if _thread_sensitive_context.get(None) is not None:
        raise RuntimeError(_OFF_LANE_ORM_CALL)
    return _submit_blocking(*_orm_handoff(), fn, *args)


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
