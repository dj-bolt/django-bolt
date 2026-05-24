"""Accept-Encoding negotiation edge cases for streaming compression.

The Rust-side parser is unit-tested directly in `src/streaming_compression.rs`;
this file exercises the same parser through the full TestClient stack so we
catch regressions in plumbing (Rust → Python → wire). One handler, many
`Accept-Encoding` values.
"""

from __future__ import annotations

import pytest

pytest.importorskip("brotli", reason="brotli package required for httpx auto-decode")

from django_bolt import BoltAPI
from django_bolt.middleware import CompressionConfig
from django_bolt.responses import StreamingResponse
from django_bolt.testing import TestClient


@pytest.fixture
def api():
    api = BoltAPI(compression=CompressionConfig(backend="brotli", gzip_fallback=True))

    @api.get("/stream")
    async def stream():
        async def gen():
            yield "negotiation-test"

        return StreamingResponse(gen(), media_type="text/plain")

    return api


def _ce(resp) -> str:
    return resp.headers.get("content-encoding", "").lower()


def test_plain_br(api):
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "br"})
        assert _ce(resp) == "br"


def test_br_with_qvalue(api):
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "br;q=0.5"})
        assert _ce(resp) == "br"


def test_br_qzero_rejects_brotli(api):
    """`br;q=0` rejects brotli — server falls back to gzip."""
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "br;q=0, gzip"})
        assert _ce(resp) == "gzip"


def test_star_accepts_brotli(api):
    """`*` with positive q accepts unmentioned codings (including brotli)."""
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "*"})
        assert _ce(resp) == "br"


def test_star_qzero_rejects_unmentioned(api):
    """`*;q=0` rejects everything not explicitly listed."""
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "gzip, *;q=0"})
        assert _ce(resp) == "gzip"


def test_capitalized_coding_matches(api):
    """Accept-Encoding coding names are case-insensitive."""
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "BR"})
        assert _ce(resp) == "br"


def test_explicit_q0_overrides_star(api):
    """`br;q=0, *` — brotli is explicitly rejected even though `*` is generous."""
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "br;q=0, *"})
        # `*` matches gzip (the fallback path), not brotli.
        assert _ce(resp) == "gzip"


def test_empty_accept_encoding_header(api):
    """An empty `Accept-Encoding` value names no codings → no compression.

    (httpx auto-injects a default `Accept-Encoding` when none is given, so
    we can't test "no header" through TestClient; the empty-string case is
    the real edge.)
    """
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": ""})
        assert _ce(resp) in ("", "identity")


def test_only_deflate(api):
    """Client only advertises an encoding we don't ship → identity."""
    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "deflate"})
        assert _ce(resp) in ("", "identity")


def test_gzip_fallback_disabled():
    """With `gzip_fallback=False`, no gzip fallback when client rejects brotli."""
    api = BoltAPI(compression=CompressionConfig(backend="brotli", gzip_fallback=False))

    @api.get("/stream")
    async def stream():
        async def gen():
            yield "no-fallback"

        return StreamingResponse(gen(), media_type="text/plain")

    with TestClient(api) as client:
        resp = client.get("/stream", headers={"Accept-Encoding": "gzip"})
        assert _ce(resp) in ("", "identity")
        assert resp.content == b"no-fallback"
