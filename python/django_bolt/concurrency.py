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
from collections.abc import Callable
from functools import partial

logger = logging.getLogger(__name__)

__all__ = ("in_orm_executor_thread", "run_in_orm_executor", "run_orm_blocking", "sync_to_thread")

# Shared default pool for generic blocking work. Passing an explicit executor
# keeps the compatibility asyncio loop and WorkerLoop from each lazily creating
# a separate default pool. Thread count is bounded and independently tunable
# from the database-aware ORM pool below.
_default_executor: concurrent.futures.ThreadPoolExecutor | None = None

# Guards lazy construction, like the ORM pool's lock below. First use was once
# confined to event loop threads; the user-load shim and the reentrant ORM
# hand-off now reach this from arbitrary threads, where two racing callers
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
            _orm_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, workers),
                thread_name_prefix="bolt_orm",
                initializer=_mark_orm_thread,
            )
        return _orm_executor


def _submit_blocking(
    executor: concurrent.futures.ThreadPoolExecutor, fn: Callable[..., object], *args: object
) -> object:
    """Submit ``fn`` to ``executor`` with the caller's context and block on it."""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args).result()


def run_orm_blocking[T](fn: Callable[..., T], *args: object) -> T:
    """Blocking counterpart of :func:`run_in_orm_executor` for callers that cannot await.

    Exists for the synchronous user-loading path: ``request.user`` is a
    ``SimpleLazyObject`` forced from code that cannot await. Same contract as
    the async variant — the query runs on the bounded ORM pool with the
    caller's contextvars visible, so a request-scoped database router applies
    to user loading exactly as it does to QuerySet evaluation.

    A caller already on an ORM pool worker (a lazy ``request.user`` forced
    while a QuerySet evaluates) runs inline: blocking on the pool would wait
    on a slot this thread is holding. The inline call still runs in a copied
    context so ContextVar writes stay scoped the same way as on the
    executor path.
    """
    if in_orm_executor_thread():
        return contextvars.copy_context().run(fn, *args)
    return _submit_blocking(_get_orm_executor(), fn, *args)


def _run_default_blocking[T](fn: Callable[..., T], *args: object) -> T:
    """Blocking hand-off to the generic default pool (internal).

    Hosts the ``asyncio.run`` shim for a custom async ``get_user``: the
    coroutine may await non-database work, which must not pin a bounded ORM
    slot for its full duration. A caller already on an ORM worker runs the
    shim inline instead — blocking on another pool while holding an ORM slot
    would let the coroutine's own ORM submission deadlock against that slot.
    """
    if in_orm_executor_thread():
        return contextvars.copy_context().run(fn, *args)
    return _submit_blocking(_get_default_executor(), fn, *args)


async def run_in_orm_executor[**P, T](fn: Callable[P, T], *args: P.args) -> T:
    """Run a framework-initiated ORM evaluation in the bounded ORM pool.

    The caller's contextvars context is carried into the pool thread —
    request-scoped state (e.g. a tenant-aware database router) must be
    visible while the QuerySet evaluates, exactly as it was under
    ``sync_to_async``.

    A caller already on a pool thread — reached through a loop nested inside
    a pool worker, i.e. a custom async ``get_user`` driven by ``asyncio.run``
    while a lazy ``request.user`` is forced from ORM work — cannot use the
    pool: submitting waits on the slot that worker is holding (never resolves
    on a one-thread pool), and running inline is impossible because the
    worker now has a running event loop, so sync ORM raises Django's
    ``SynchronousOnlyOperation``. That reentrant case crosses to the generic
    default pool instead: one transient connection outside the ORM budget on
    a rare path, in exchange for neither deadlocking nor tripping the
    async-unsafe check.
    """
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    if in_orm_executor_thread():
        return await loop.run_in_executor(_get_default_executor(), ctx.run, fn, *args)
    return await loop.run_in_executor(_get_orm_executor(), ctx.run, fn, *args)


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
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    if kwargs:
        return await loop.run_in_executor(_get_default_executor(), partial(ctx.run, fn, *args, **kwargs))
    return await loop.run_in_executor(_get_default_executor(), ctx.run, fn, *args)
