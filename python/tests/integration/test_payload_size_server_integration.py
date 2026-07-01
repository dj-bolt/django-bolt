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

pytestmark = pytest.mark.server_integration


API_BODY = """
import msgspec


class Blob(msgspec.Struct):
    data: str


@api.post("/sink")
async def sink(blob: Blob) -> dict:
    return {"len": len(blob.data)}
"""

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
    project = make_server_project(project_api_body=API_BODY, settings_extra=SETTINGS_EXTRA)
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


def test_chunked_under_limit_is_not_size_rejected(make_server_project):
    """A small chunked body must not be size-rejected (control for the above)."""
    project = make_server_project(project_api_body=API_BODY, settings_extra=SETTINGS_EXTRA)
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
