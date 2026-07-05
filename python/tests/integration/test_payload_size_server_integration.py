"""Real-server coverage for the streaming payload-size overflow branch.

The in-process payload tests (tests/test_payload_size.py) use TestClient, which
always sets Content-Length, so they only exercise the early Content-Length 413
fast-reject. They never hit the streaming ``total_read > max_payload_size`` branch
in src/handler.rs that protects chunked uploads that omit Content-Length.

These tests send a real chunked request (httpx streams a generator body with
Transfer-Encoding: chunked, no Content-Length) to a live runbolt server and
assert the streaming overflow branch returns 413.
"""

from __future__ import annotations

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration


# Keep the limit small so the body crosses it after a couple of chunks.
SETTINGS_EXTRA = "BOLT_MAX_UPLOAD_SIZE = 2000"


def _chunks(total: int, chunk: int = 1000):
    sent = 0
    while sent < total:
        n = min(chunk, total - sent)
        sent += n
        yield b"x" * n


def test_chunked_over_limit_rejected_with_413(make_server_project):
    """A chunked body (no Content-Length) over the limit must 413 mid-stream."""
    project = make_server_project(api_module=app_module("payload_sink"), settings_extra=SETTINGS_EXTRA)
    with project.start() as server:
        # 50 KB streamed as chunks → no Content-Length → only the streaming
        # total_read check can stop it. (Garbage body: the size check fires
        # before JSON validation, so this asserts the overflow branch, not 422.)
        response = server.request(
            "POST",
            "/sink",
            content=_chunks(50_000),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413, (
        f"chunked over-limit body must 413 via the streaming check, got {response.status_code}"
    )


_BOUNDARY = "boltaggregateboundary"


def _multipart_chunks(payload: bytes, chunk: int = 1000):
    """Yield a hand-rolled multipart body in chunks (streamed → no Content-Length)."""
    body = (
        (
            f"--{_BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="avatar"; filename="blob.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{_BOUNDARY}--\r\n".encode()
    )
    for i in range(0, len(body), chunk):
        yield body[i : i + chunk]


def test_chunked_multipart_over_aggregate_limit_rejected_with_413(make_server_project):
    """Chunked multipart over BOLT_MAX_UPLOAD_SIZE must 413 via the aggregate check.

    No Content-Length (streamed) → the fast-reject can't fire, and the per-file
    File(max_size=100_000) is far above the 50 KB payload — so only the aggregate
    max_total_size bound inside parse_multipart can produce the 413. Regression
    for the master-merge that dropped the max_total_size wiring from the
    production parse_multipart call.
    """
    project = make_server_project(api_module=app_module("payload_sink"), settings_extra=SETTINGS_EXTRA)
    with project.start() as server:
        response = server.request(
            "POST",
            "/upload",
            content=_multipart_chunks(b"x" * 50_000),
            headers={"content-type": f"multipart/form-data; boundary={_BOUNDARY}"},
        )
    assert response.status_code == 413, (
        f"chunked multipart over the aggregate limit must 413, got {response.status_code}: {response.text[:200]}"
    )


def test_chunked_multipart_under_aggregate_limit_succeeds(make_server_project):
    """A small chunked multipart body must not be size-rejected (control)."""
    project = make_server_project(api_module=app_module("payload_sink"), settings_extra=SETTINGS_EXTRA)
    with project.start() as server:
        response = server.request(
            "POST",
            "/upload",
            content=_multipart_chunks(b"y" * 500),
            headers={"content-type": f"multipart/form-data; boundary={_BOUNDARY}"},
        )
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text[:200]}"
    assert response.json()["size"] == 500


def test_chunked_under_limit_is_not_size_rejected(make_server_project):
    """A small chunked body must not be size-rejected (control for the above)."""
    project = make_server_project(api_module=app_module("payload_sink"), settings_extra=SETTINGS_EXTRA)
    with project.start() as server:
        # Valid small JSON, streamed chunked and well under the 2000-byte limit.
        body = b'{"data":"' + b"y" * 200 + b'"}'
        response = server.request(
            "POST",
            "/sink",
            content=iter([body]),
            headers={"content-type": "application/json"},
        )
    # Must not be a 413 (size). 200 expected; never the payload-too-large path.
    assert response.status_code != 413, "under-limit chunked body must not be size-rejected"
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text[:200]}"
