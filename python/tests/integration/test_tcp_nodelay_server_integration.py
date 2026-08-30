"""Multi-write responses must not hit the Nagle + delayed-ACK stall (~40 ms per response)."""

from __future__ import annotations

import time

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration


def test_streaming_responses_are_not_delayed_by_nagle(make_server_project):
    project = make_server_project(api_module=app_module("compression_sse"))

    with project.start() as server:
        server.get("/sse")  # warm the keep-alive connection
        started = time.perf_counter()
        for _ in range(10):
            response = server.get("/sse")
            assert response.status_code == 200
        elapsed = time.perf_counter() - started

    # With Nagle on, each response pays ~41 ms (10 requests ≈ 410 ms).
    assert elapsed < 0.2, f"10 SSE responses took {elapsed:.3f}s"
