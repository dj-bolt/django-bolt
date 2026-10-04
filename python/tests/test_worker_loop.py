"""Unit coverage for WorkerLoop helpers that lean on private asyncio APIs.

``_with_running_loop`` uses ``asyncio.events._get_running_loop`` /
``_set_running_loop``. These are stable across CPython 3.12-3.14 but private;
this test fails loudly if a future CPython moves them.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from django_bolt._worker_loop import _with_running_loop


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
