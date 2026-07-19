"""Thread pool execution utilities for sync handlers.

This module provides utilities to run synchronous callables in a thread pool,
enabling concurrent execution of I/O-bound sync handlers without blocking
the async event loop.

Inspired by Litestar's concurrency module.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from collections.abc import Callable
from functools import partial

logger = logging.getLogger(__name__)

__all__ = ("sync_to_thread", "run_in_orm_executor")

# Shared default pool for generic blocking work. Passing an explicit executor
# keeps the compatibility asyncio loop and WorkerLoop from each lazily creating
# a separate default pool. Thread count is bounded and independently tunable
# from the database-aware ORM pool below.
_default_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _get_default_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _default_executor
    if _default_executor is None:
        raw = os.environ.get("DJANGO_BOLT_EXECUTOR_THREADS")
        platform_default = min(32, (os.cpu_count() or 1) + 4)
        try:
            workers = int(raw) if raw else platform_default
        except ValueError:
            workers = platform_default
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


def _get_orm_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _orm_executor
    if _orm_executor is None:
        raw = os.environ.get("DJANGO_BOLT_ORM_THREADS")
        try:
            workers = int(raw) if raw else _default_orm_workers()
        except ValueError:
            workers = _default_orm_workers()
        _orm_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="bolt_orm"
        )
    return _orm_executor


async def run_in_orm_executor[**P, T](fn: Callable[P, T], *args: P.args) -> T:
    """Run a framework-initiated ORM evaluation in the bounded ORM pool."""
    return await asyncio.get_running_loop().run_in_executor(_get_orm_executor(), fn, *args)


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
    # run_in_executor only forwards positional args — keyword args (e.g. Rust
    # prebound keyword-bound params) must be bound via partial.
    loop = asyncio.get_running_loop()
    if kwargs:
        return await loop.run_in_executor(_get_default_executor(), partial(fn, *args, **kwargs))
    return await loop.run_in_executor(_get_default_executor(), fn, *args)
