"""Unit coverage for WorkerLoop helpers that lean on private asyncio APIs.

``_with_running_loop`` uses ``asyncio.events._get_running_loop`` /
``_set_running_loop``. These are stable across CPython 3.12-3.14 but private;
this test fails loudly if a future CPython moves them.
"""

from __future__ import annotations

import asyncio
import contextvars
import socket
import subprocess
import sys

import pytest

from django_bolt import BoltAPI
from django_bolt._worker_loop import _with_running_loop
from django_bolt.testing import TestClient


def test_with_running_loop_installs_and_restores():
    loop = asyncio.new_event_loop()
    try:
        assert _with_running_loop(loop, asyncio.get_running_loop) is loop
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
    finally:
        loop.close()


def test_with_running_loop_restores_on_exception():
    loop = asyncio.new_event_loop()

    def boom():
        raise ValueError("boom")

    try:
        with pytest.raises(ValueError):
            _with_running_loop(loop, boom)
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
    finally:
        loop.close()


# The loops of the test workers live as long as the process. The first request
# of this script makes them while a test pool replaces the default pool, as the
# `single_thread_pool` fixture of test_broken_connection_recovery.py does.
_REPLACED_POOL = """
import asyncio
import concurrent.futures

import django
from django.conf import settings

settings.configure(DEBUG=False)
django.setup()

from django_bolt import BoltAPI, concurrency
from django_bolt.testing import TestClient

api = BoltAPI()


@api.get("/offload")
async def offload():
    return {"value": await asyncio.get_running_loop().run_in_executor(None, int, "7")}


pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
concurrency._default_executor = pool
with TestClient(api) as client:
    assert client.get("/offload").json() == {"value": 7}
pool.shutdown()
concurrency._default_executor = None

with TestClient(api) as client:
    response = client.get("/offload")
assert response.status_code == 200, response.text
assert response.json() == {"value": 7}
"""


def test_a_loop_uses_the_default_pool_of_the_moment():
    """A loop that outlives a pool must not keep it: run_in_executor(None) uses the current pool."""
    result = subprocess.run([sys.executable, "-c", _REPLACED_POOL], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="WorkerLoop file-descriptor watching requires Unix")
def test_descriptor_watchers_accept_and_use_a_context():
    """Python 3.15 transports call loop._add_reader/_add_writer with context=..."""
    api = BoltAPI()
    var = contextvars.ContextVar("var", default="unset")

    @api.get("/watch")
    async def watch():
        loop = asyncio.get_running_loop()
        left, right = socket.socketpair()
        seen = {}
        done = asyncio.Event()

        def on_readable():
            seen["reader"] = var.get()
            loop._remove_reader(left)
            done.set()

        def on_writable():
            seen["writer"] = var.get()
            loop._remove_writer(right)
            right.send(b"x")

        context = contextvars.copy_context()
        context.run(var.set, "watched")
        try:
            loop._add_reader(left, on_readable, context=context)
            loop._add_writer(right, on_writable, context=context)
            await asyncio.wait_for(done.wait(), 5)
        finally:
            loop._remove_reader(left)
            loop._remove_writer(right)
            left.close()
            right.close()
        return seen

    with TestClient(api) as client:
        response = client.get("/watch")
    assert response.status_code == 200, response.text
    assert response.json() == {"reader": "watched", "writer": "watched"}


def test_a_timer_callback_runs_in_the_context_that_the_caller_gives():
    """``call_later(..., context=...)`` runs the callback in that context.

    With no ``context``, the callback runs in a copy of the context of the caller.
    """
    api = BoltAPI()
    var = contextvars.ContextVar("var", default="unset")

    @api.get("/timers")
    async def timers():
        loop = asyncio.get_running_loop()
        seen = {}

        def record(name):
            seen[name] = var.get()

        context = contextvars.copy_context()
        context.run(var.set, "given")
        var.set("caller")
        loop.call_later(0.01, record, "with-context", context=context)
        loop.call_later(0.01, record, "no-context")
        await asyncio.sleep(0.1)
        return seen

    with TestClient(api) as client:
        response = client.get("/timers")
    assert response.status_code == 200, response.text
    assert response.json() == {"with-context": "given", "no-context": "caller"}


def test_a_callback_that_raises_system_exit_goes_to_the_exception_handler_of_the_loop():
    """``Handle._run`` lets SystemExit escape. The loop reports it, and the worker serves the next request."""
    api = BoltAPI()

    @api.get("/exit-in-callback")
    async def exit_in_callback():
        loop = asyncio.get_running_loop()
        reports = []

        def stop():
            raise SystemExit(3)

        # The loop lives as long as the process, so put its handler back.
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: reports.append(context))
        try:
            handle = loop.call_soon(stop)
            await asyncio.sleep(0.05)
        finally:
            loop.set_exception_handler(previous)
        return [
            {
                "message": report["message"],
                "exception": repr(report["exception"]),
                "is_the_handle": report["handle"] is handle,
            }
            for report in reports
        ]

    with TestClient(api) as client:
        response = client.get("/exit-in-callback")
        next_response = client.get("/exit-in-callback")
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"message": "Exception in WorkerLoop callback", "exception": "SystemExit(3)", "is_the_handle": True}
    ]
    assert next_response.status_code == 200, next_response.text
