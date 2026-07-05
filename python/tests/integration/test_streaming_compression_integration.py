"""Server-integration tests for default-on streaming compression.

Run runbolt as a real subprocess and make requests over a real TCP socket so
we catch startup-time wiring issues that the in-process TestClient can't see
(CompressionConfig threading from Python → Rust AppState, middleware order,
codec negotiation on the production code path).

Each test verifies the default-on path: a plain `EventSourceResponse(gen())`
or `StreamingResponse(gen())` with no `compress=` kwarg and no `@no_compress`,
relying entirely on the default `BoltAPI().compression = CompressionConfig()`.

httpx auto-decodes `Content-Encoding: br`/`gzip` when the corresponding
decoder package is installed, so `resp.content` is the *decoded* bytes. We
assert on the `Content-Encoding` header for wire compression and inspect the
decoded body for payload integrity.
"""

from __future__ import annotations

import pytest

from .apps import app_module

pytest.importorskip("brotli", reason="brotli package required for httpx auto-decode")

pytestmark = pytest.mark.server_integration


def test_default_on_brotli_sse_end_to_end(make_server_project):
    """Bare `EventSourceResponse(gen())` with default `BoltAPI()` → brotli SSE."""
    project = make_server_project(api_module=app_module("streaming_compression"))
    with project.start(startup_path="/health") as server:
        resp = server.get("/events", headers={"Accept-Encoding": "br"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "br"

    decoded = resp.content
    for i in range(5):
        assert f'data: {{"i": {i}}}\n\n'.encode() in decoded or f'data: {{"i":{i}}}\n\n'.encode() in decoded, (
            f"event i={i} not found in decoded SSE stream"
        )


def test_default_on_gzip_fallback_sse(make_server_project):
    """Client only accepts gzip → server falls back to gzip per-chunk."""
    project = make_server_project(api_module=app_module("streaming_compression"))
    with project.start(startup_path="/health") as server:
        resp = server.get("/events", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "gzip"

    decoded = resp.content
    for i in range(5):
        assert f'data: {{"i": {i}}}\n\n'.encode() in decoded or f'data: {{"i":{i}}}\n\n'.encode() in decoded


def test_no_compress_decorator_opts_out(make_server_project):
    """`@no_compress` on a streaming handler skips wire compression."""
    project = make_server_project(api_module=app_module("streaming_compression"))
    with project.start(startup_path="/health") as server:
        resp = server.get("/events/raw", headers={"Accept-Encoding": "br"})

    assert resp.status_code == 200
    ce = resp.headers.get("content-encoding", "").lower()
    # Middleware strips the identity marker before sending.
    assert ce in ("", "identity")


def test_generic_streaming_response_auto_compresses(make_server_project):
    """Default-on applies to generic `StreamingResponse`, not just SSE."""
    project = make_server_project(api_module=app_module("streaming_compression"))
    with project.start(startup_path="/health") as server:
        resp = server.get("/text", headers={"Accept-Encoding": "br"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "br"
    assert resp.content == b"alpha-beta-gamma"
