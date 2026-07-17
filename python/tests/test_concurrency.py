"""Tests for sync_to_thread thread-pool execution."""

from __future__ import annotations

import asyncio
import threading

import pytest

from django_bolt.concurrency import sync_to_thread


def test_returns_value():
    """Positional args are forwarded and the return value comes back."""
    assert asyncio.run(sync_to_thread(lambda a, b: a + b, 2, 3)) == 5


def test_propagates_exception():
    """Exceptions raised in the worker propagate to the caller."""

    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        asyncio.run(sync_to_thread(boom))


def test_runs_off_calling_thread():
    """The callable executes on a worker thread, not the caller's."""
    caller = threading.get_ident()
    worker = asyncio.run(sync_to_thread(threading.get_ident))
    assert worker != caller


def test_runs_concurrently():
    """Blocking calls run in parallel rather than serializing on the loop."""
    barrier = threading.Barrier(2, timeout=5)

    async def main():
        # Both calls must reach the barrier for it to release; if they ran
        # serially the first wait() would time out and raise BrokenBarrierError.
        await asyncio.gather(sync_to_thread(barrier.wait), sync_to_thread(barrier.wait))

    asyncio.run(main())
