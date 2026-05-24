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

pytest.importorskip("brotli", reason="brotli package required for httpx auto-decode")

pytestmark = pytest.mark.server_integration


def _make_streaming_project(make_server_project):
    return make_server_project(
        project_api_body="""
        from django_bolt.responses import EventSourceResponse, StreamingResponse
        from django_bolt.middleware import no_compress

        @api.get("/events")
        async def stream():
            async def gen():
                for i in range(5):
                    yield {"i": i}
            return EventSourceResponse(gen())

        @api.get("/events/raw")
        @no_compress
        async def raw_stream():
            async def gen():
                for i in range(3):
                    yield {"i": i}
            return EventSourceResponse(gen())

        @api.get("/text")
        async def text_stream():
            async def gen():
                for chunk in ("alpha-", "beta-", "gamma"):
                    yield chunk
            return StreamingResponse(gen(), media_type="text/plain")
        """,
    )


def test_default_on_brotli_sse_end_to_end(make_server_project):
    """Bare `EventSourceResponse(gen())` with default `BoltAPI()` → brotli SSE."""
    project = _make_streaming_project(make_server_project)
    with project.start(startup_path="/health") as server:
        resp = server.get("/events", headers={"Accept-Encoding": "br"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "br"

    decoded = resp.content
    for i in range(5):
        assert (
            f'data: {{"i": {i}}}\n\n'.encode() in decoded
            or f'data: {{"i":{i}}}\n\n'.encode() in decoded
        ), f"event i={i} not found in decoded SSE stream"


def test_default_on_gzip_fallback_sse(make_server_project):
    """Client only accepts gzip → server falls back to gzip per-chunk."""
    project = _make_streaming_project(make_server_project)
    with project.start(startup_path="/health") as server:
        resp = server.get("/events", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "gzip"

    decoded = resp.content
    for i in range(5):
        assert (
            f'data: {{"i": {i}}}\n\n'.encode() in decoded
            or f'data: {{"i":{i}}}\n\n'.encode() in decoded
        )


def test_no_compress_decorator_opts_out(make_server_project):
    """`@no_compress` on a streaming handler skips wire compression."""
    project = _make_streaming_project(make_server_project)
    with project.start(startup_path="/health") as server:
        resp = server.get("/events/raw", headers={"Accept-Encoding": "br"})

    assert resp.status_code == 200
    ce = resp.headers.get("content-encoding", "").lower()
    # Middleware strips the identity marker before sending.
    assert ce in ("", "identity")


def test_generic_streaming_response_auto_compresses(make_server_project):
    """Default-on applies to generic `StreamingResponse`, not just SSE."""
    project = _make_streaming_project(make_server_project)
    with project.start(startup_path="/health") as server:
        resp = server.get("/text", headers={"Accept-Encoding": "br"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "br"
    assert resp.content == b"alpha-beta-gamma"
