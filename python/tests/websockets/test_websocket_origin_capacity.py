"""Custom-origin admission must not starve the normal Bolt worker pool."""
from __future__ import annotations

import asyncio
import contextvars
import subprocess
import sys
import threading

import pytest

from django_bolt.websocket import origin_policy
from django_bolt.websocket.origin_policy import OriginOptions, resolve_origin_options

CTX = {"path": "/ws", "query_string": "", "headers": {}}


def checked_for(validator):
    policy = resolve_origin_options(
        OriginOptions(policy="custom", validator=validator), read_settings=False
    )
    return policy._bolt_websocket_origin_validator


@pytest.mark.asyncio
async def test_custom_async_validation_has_bounded_admission_and_releases_on_cancel():
    waiting = asyncio.Event()

    async def held(origin, _ctx):
        if origin == "https://hold.example":
            await waiting.wait()
        return True

    checked = checked_for(held)
    tasks = [asyncio.create_task(checked("https://hold.example", CTX)) for _ in range(
        origin_policy._MAX_PENDING_CUSTOM_ORIGINS
    )]
    try:
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError, match="Too many pending"):
            await checked("https://trusted.example", CTX)
        tasks[0].cancel()
        assert isinstance((await asyncio.gather(tasks[0], return_exceptions=True))[0], asyncio.CancelledError)
        # Cancelling an async validator frees exactly one admission slot.
        tasks.append(asyncio.create_task(checked("https://hold.example", CTX)))
        await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError, match="Too many pending"):
            await checked("https://trusted.example", CTX)
    finally:
        waiting.set()
        assert all(await asyncio.gather(*tasks[1:]))
    assert await checked("https://trusted.example", CTX)


@pytest.mark.asyncio
async def test_slow_sync_validators_cannot_starve_default_workers_or_free_slots_on_cancel():
    unblock = threading.Event()
    started = 0
    guard = threading.Lock()

    def held(_origin, _ctx):
        nonlocal started
        with guard:
            started += 1
        unblock.wait(timeout=10)
        return True

    checked = checked_for(held)
    tasks = [asyncio.create_task(checked("https://trusted.example", CTX)) for _ in range(
        origin_policy._SYNC_ORIGIN_WORKERS
    )]
    try:
        for _ in range(300):
            with guard:
                if started == origin_policy._SYNC_ORIGIN_WORKERS:
                    break
            await asyncio.sleep(0.01)
        assert started == origin_policy._SYNC_ORIGIN_WORKERS
        with pytest.raises(RuntimeError, match="Too many synchronous"):
            await checked("https://trusted.example", CTX)
        # Explicit cancellation must NOT release a worker still inside held().
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        with pytest.raises(RuntimeError, match="Too many synchronous"):
            await checked("https://trusted.example", CTX)
        # A shared-pool implementation would deadlock or starve this job.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "normal handler"), 2) == "normal handler"
    finally:
        unblock.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A thread completion callback can run just after awaiting the wrapper.
        for _ in range(200):
            if origin_policy._sync_origin_slots._value == origin_policy._SYNC_ORIGIN_WORKERS:
                break
            await asyncio.sleep(0.01)
    assert await checked("https://trusted.example", CTX)


@pytest.mark.asyncio
async def test_async_origin_timeout_cancels_validator_and_releases_admission(monkeypatch):
    monkeypatch.setattr(origin_policy, "_PYTHON_ORIGIN_TIMEOUT_SECONDS", 0.05)
    cancelled = asyncio.Event()

    async def blocked(_origin, _ctx):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    checked = checked_for(blocked)
    with pytest.raises(TimeoutError):
        await checked("https://trusted.example", CTX)
    assert cancelled.is_set()
    # This would fail if Python retained the permit after timed-out calls.
    assert await checked_for(lambda _origin, _ctx: True)("https://trusted.example", CTX)


@pytest.mark.asyncio
async def test_sync_origin_executor_preserves_contextvars():
    marker = contextvars.ContextVar("ws_origin_marker")
    token = marker.set("handshake-context")
    try:
        checked = checked_for(lambda _origin, _ctx: marker.get(None) == "handshake-context")
        assert await checked("https://trusted.example", CTX)
    finally:
        marker.reset(token)


def test_blocked_sync_validator_does_not_prevent_process_exit():
    # Isolate this check: a non-daemon ThreadPoolExecutor would stall the
    # subprocess at interpreter exit until the blocked callback returns.
    code = """
import threading
from django_bolt.websocket.origin_policy import _get_sync_origin_executor
ready = threading.Event()
def blocked():
    ready.set()
    threading.Event().wait(30)
_get_sync_origin_executor().submit(blocked)
assert ready.wait(3), 'origin worker did not start'
print('origin worker started', flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        timeout=8, check=True,
    )
    assert "origin worker started" in result.stdout



@pytest.mark.asyncio
async def test_origin_worker_start_failure_does_not_orphan_jobs(monkeypatch):
    executor = origin_policy._DaemonOriginExecutor()
    monkeypatch.setattr(origin_policy, "_sync_origin_executor", executor)
    invoked = []

    def validate(_origin, _context):
        invoked.append(True)
        return True

    checked = checked_for(validate)
    original_start = threading.Thread.start

    def failing_start(thread):
        if thread.name.startswith("bolt_ws_origin_"):
            raise RuntimeError("simulated thread start failure")
        return original_start(thread)

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", failing_start)
        for _ in range(origin_policy._SYNC_ORIGIN_WORKERS + 1):
            with pytest.raises(RuntimeError, match="simulated thread start failure"):
                await checked("https://trusted.example", CTX)

    assert executor._threads == []
    assert executor._outstanding == 0
    assert executor._jobs.empty()
    assert invoked == []
    assert origin_policy._sync_origin_slots._value == origin_policy._SYNC_ORIGIN_WORKERS

    # The same executor must recover, without executing any orphaned jobs.
    assert await checked("https://trusted.example", CTX)
    assert invoked == [True]


@pytest.mark.asyncio
async def test_origin_absolute_deadline_includes_python_scheduling_delay(monkeypatch):
    monkeypatch.setattr(origin_policy, "_PYTHON_ORIGIN_TIMEOUT_SECONDS", 0.15)
    entered = []
    loop = asyncio.get_running_loop()

    async def validate(_origin, _context):
        entered.append(True)
        await asyncio.sleep(0.4)
        return True

    checked = checked_for(validate)

    # Deadline already expired before Python executes the coroutine.
    with pytest.raises(TimeoutError):
        await checked("https://trusted.example", CTX, loop.time() - 0.20)
    assert entered == []

    # Only the remaining budget is available, not another full 0.15 s.
    with pytest.raises(TimeoutError):
        await checked("https://trusted.example", CTX, loop.time() - 0.08)
    assert entered == [True]

    # Both timeout paths must free their Python admission slots.
    assert await checked_for(
        lambda _origin, _context: True
    )("https://trusted.example", CTX)
    assert (
        origin_policy._pending_custom_origins._value
        == origin_policy._MAX_PENDING_CUSTOM_ORIGINS
    )
