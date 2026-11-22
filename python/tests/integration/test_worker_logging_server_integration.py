"""The workers of runbolt write their log records when LOGGING is not set (issue #909)."""

from __future__ import annotations

import sys
import time

import httpx
import pytest

from .apps import app_module
from .apps.worker_logging import SHUTDOWN_RECORDS
from .helpers import RunningServer


def _wait_for_each_worker(server: RunningServer, processes: int, timeout: float = 15.0) -> None:
    """Wait until each worker answers a request, so each worker ran its lifespan startup.

    The first answer from ``/health`` comes from one worker only. A worker that
    gets SIGTERM before its lifespan startup writes no shutdown record.
    """
    pids: set[int] = set()
    deadline = time.time() + timeout
    while len(pids) < processes and time.time() < deadline:
        # A new connection for each request, so the kernel can pick each worker.
        pids.add(httpx.get(server.url("/pid"), timeout=5).json()["pid"])
    assert len(pids) == processes, f"only {len(pids)} of {processes} workers answered: {pids}"


@pytest.mark.server_integration
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess runs only on Linux.")
@pytest.mark.parametrize("processes", [1, 2])
def test_worker_writes_its_log_records_without_logging_setting(make_server_project, processes):
    project = make_server_project(api_module=app_module("worker_logging"))
    with project.start(processes=processes) as server:
        _wait_for_each_worker(server, processes)
        assert server.get("/warn").status_code == 200
        assert server.get("/boom").status_code == 500
        stdout, stderr = server.stop()

    output = stdout + stderr
    assert "MARKER-WARNING-FROM-HANDLER" in output, output
    assert "RuntimeError: MARKER-BOOM" in output, output
    # Each worker writes its last record before it exits.
    assert output.count(f"MARKER-AT-SHUTDOWN-{SHUTDOWN_RECORDS - 1}\n") == processes, output[-2000:]
