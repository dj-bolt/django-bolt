"""Shut down a test thread pool without leaking its database connections."""

from __future__ import annotations

import concurrent.futures
import threading

from django.db import connections


def shutdown_closing_connections(pool: concurrent.futures.ThreadPoolExecutor) -> None:
    """Close the Django connections of each pool thread, then shut the pool down.

    A pool thread keeps its connection in thread-local storage. A thread that
    exits with the connection open leaves it to the garbage collector, which
    reports a ResourceWarning under a later, unrelated test.

    One task runs on each thread: each task waits until every worker holds
    one, so no thread runs two of them.
    """
    workers = pool._max_workers
    barrier = threading.Barrier(workers, timeout=5)

    def close_on_this_thread() -> None:
        barrier.wait()
        connections.close_all()

    try:
        for future in [pool.submit(close_on_this_thread) for _ in range(workers)]:
            future.result()
    finally:
        # A close error still shows, and the pool threads still stop.
        pool.shutdown(wait=True)
