"""The workers of runbolt write their log records when LOGGING is not set (issue #909)."""

from __future__ import annotations

import sys

import pytest

from .apps import app_module
from .apps.worker_logging import SHUTDOWN_RECORDS


@pytest.mark.server_integration
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess runs only on Linux.")
@pytest.mark.parametrize("processes", [1, 2])
def test_worker_writes_its_log_records_without_logging_setting(make_server_project, processes):
    project = make_server_project(api_module=app_module("worker_logging"))
    with project.start(processes=processes) as server:
        assert server.get("/warn").status_code == 200
        assert server.get("/boom").status_code == 500
        stdout, stderr = server.stop()

    output = stdout + stderr
    assert "MARKER-WARNING-FROM-HANDLER" in output, output
    assert "RuntimeError: MARKER-BOOM" in output, output
    # Each worker writes its last record before it exits.
    assert output.count(f"MARKER-AT-SHUTDOWN-{SHUTDOWN_RECORDS - 1}\n") == processes, output[-2000:]
