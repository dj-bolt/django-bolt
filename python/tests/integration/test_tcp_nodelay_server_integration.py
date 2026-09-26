"""Multi-write responses must not hit the Nagle + delayed-ACK stall (~40 ms per response)."""

from __future__ import annotations

import time

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration

# Linux holds a delayed ACK for 40 ms minimum. With Nagle on, each response
# waits for that ACK, so no response can finish faster than 40 ms.
NAGLE_STALL_S = 0.040
SAMPLES = 20


def test_streaming_responses_are_not_delayed_by_nagle(make_server_project):
    project = make_server_project(api_module=app_module("compression_sse"))

    with project.start() as server:
        server.get("/sse")  # warm the keep-alive connection
        latencies = []
        for _ in range(SAMPLES):
            started = time.perf_counter()
            response = server.get("/sse")
            latencies.append(time.perf_counter() - started)
            assert response.status_code == 200

    # A slow runner makes some responses slow, but not all of them.
    # The Nagle stall makes all responses slow. Thus, the test checks the fastest response.
    fastest = min(latencies)
    samples_ms = ", ".join(f"{latency * 1000:.1f}" for latency in latencies)
    assert fastest < NAGLE_STALL_S / 2, (
        f"fastest of {SAMPLES} SSE responses took {fastest * 1000:.1f} ms"
        f" (Nagle stall is {NAGLE_STALL_S * 1000:.0f} ms); all samples in ms: {samples_ms}"
    )
