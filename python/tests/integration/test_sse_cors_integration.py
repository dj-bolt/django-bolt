from __future__ import annotations

import time

import httpx
import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration


def _make_sse_project(make_server_project):
    return make_server_project(api_module=app_module("sse_cors"))


def test_async_sse_has_cors_headers(make_server_project):
    project = _make_sse_project(make_server_project)

    with project.start() as server:
        response = server.get("/sse-cors-async", headers={"Origin": "https://example.com"})

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert response.headers.get("access-control-allow-origin") == "https://example.com"
    vary_headers = ", ".join(response.headers.get_list("vary")).lower()
    assert "origin" in vary_headers
    assert "data: message-0" in response.text
    assert "data: message-1" in response.text
    assert "data: message-2" in response.text


def test_sync_sse_has_cors_headers(make_server_project):
    project = _make_sse_project(make_server_project)

    with project.start() as server:
        response = server.get("/sse-cors-sync", headers={"Origin": "https://sync-app.com"})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://sync-app.com"


def test_sse_cors_credentials(make_server_project):
    project = _make_sse_project(make_server_project)

    with project.start() as server:
        response = server.get("/sse-cors-credentials", headers={"Origin": "https://secure.com"})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://secure.com"
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_sse_without_cors_decorator_has_no_headers(make_server_project):
    project = _make_sse_project(make_server_project)

    with project.start() as server:
        response = server.get("/sse-no-cors", headers={"Origin": "https://example.com"})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") is None


def test_sse_streams_in_real_time(make_server_project):
    project = _make_sse_project(make_server_project)

    with project.start() as server, httpx.Client(timeout=10, headers={"Accept-Encoding": "identity"}) as client:
        timestamps: list[float] = []
        with client.stream("GET", server.url("/sse-timing")) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")
            buffer = bytearray()
            for chunk in response.iter_raw(chunk_size=1):
                buffer.extend(chunk)
                if not buffer.endswith(b"\n\n"):
                    continue
                event = buffer.decode("utf-8")
                if event.startswith("data: timing-"):
                    timestamps.append(time.monotonic())
                buffer.clear()
                if len(timestamps) == 3:
                    break

    assert len(timestamps) == 3
    deltas = [timestamps[index] - timestamps[index - 1] for index in range(1, len(timestamps))]
    assert all(delta >= 0.12 for delta in deltas), deltas
