"""The harness drains the output of a ``runbolt`` server while it runs.

A server that writes more than the pipe buffer of the OS must not block. The
harness still returns all of the output from ``stop()``.
"""

from __future__ import annotations

import time

import httpx
import pytest

from .apps import app_module
from .apps.noisy_output import MESSAGE

REQUESTS = 40  # 40 x about 5 KB: well over a 64 KB pipe buffer, on each pipe


@pytest.mark.server_integration
def test_a_server_that_writes_more_than_the_pipe_buffer_keeps_answering(make_server_project):
    project = make_server_project(api_module=app_module("noisy_output"))
    with project.start() as server:
        with httpx.Client(base_url=server.base_url, timeout=5) as client:
            for _ in range(REQUESTS):
                assert client.get("/boom").status_code == 500
                assert client.get("/shout").status_code == 200

            started = time.perf_counter()
            assert client.get("/health").status_code == 200
            assert time.perf_counter() - started < 1

        stdout, stderr = server.stop()

    # stop() still returns all of the output.
    assert stdout.count(MESSAGE) == REQUESTS
    assert stderr.count(MESSAGE) >= REQUESTS
