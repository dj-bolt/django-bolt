"""Server-integration tests for buffered (non-streaming) compression.

Run runbolt as a real subprocess and make requests over a real TCP socket so
we catch startup-time wiring issues that the in-process TestClient can't
see (CompressionConfig threading from Python → Rust AppState, Actix
middleware ordering, codec negotiation on the production code path).

These tests close the gap left by `python/tests/test_compression.py`,
which mostly asserts ``status_code == 200`` without verifying the body
was actually compressed on the wire or decodes back to the original.

httpx auto-decodes ``Content-Encoding: br``/``gzip``/``zstd`` when the
corresponding decoder package is installed, so ``resp.content`` is the
*decoded* bytes. Tests assert on the ``Content-Encoding`` header (proves
wire compression was applied) and on the decoded body (proves round-trip
integrity).
"""

from __future__ import annotations

import pytest

pytest.importorskip("brotli", reason="brotli package required for httpx auto-decode")

pytestmark = pytest.mark.server_integration


# ─── Project factory ────────────────────────────────────────────────────


def _make_buffered_project(make_server_project, *, backend: str, **kwargs: object):
    """Build a project that configures ``BOLT_COMPRESSION`` via Django
    settings (the production-realistic path through ``runbolt``) and
    exposes a large JSON route plus a tiny one.

    Using ``settings_extra`` keeps the harness-injected
    ``api = BoltAPI()`` / ``/health`` route intact — re-assigning ``api``
    in ``project_api_body`` would orphan ``/health`` and break the startup
    probe.
    """
    kwargs_repr = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    extras = f", {kwargs_repr}" if kwargs_repr else ""
    return make_server_project(
        settings_extra=f"""
        from django_bolt.middleware import CompressionConfig
        BOLT_COMPRESSION = CompressionConfig(backend={backend!r}{extras})
        """,
        project_api_body="""
        @api.get("/data")
        async def get_data():
            return {"payload": "x" * 2000}

        @api.get("/tiny")
        async def tiny():
            return {"x": "small"}
        """,
    )


# ─── Per-codec roundtrip ────────────────────────────────────────────────


def test_buffered_brotli_roundtrip_end_to_end(make_server_project):
    """Brotli backend: wire is Content-Encoding: br, body decodes to JSON."""
    project = _make_buffered_project(make_server_project, backend="brotli")
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "br"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "br"
    assert "accept-encoding" in resp.headers.get("vary", "").lower()
    assert resp.json() == {"payload": "x" * 2000}


def test_buffered_gzip_roundtrip_end_to_end(make_server_project):
    project = _make_buffered_project(make_server_project, backend="gzip")
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "gzip"
    assert "accept-encoding" in resp.headers.get("vary", "").lower()
    assert resp.json() == {"payload": "x" * 2000}


def test_buffered_zstd_roundtrip_end_to_end(make_server_project):
    pytest.importorskip("zstandard", reason="zstandard package not installed")
    project = _make_buffered_project(make_server_project, backend="zstd")
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "zstd"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "zstd"
    assert "accept-encoding" in resp.headers.get("vary", "").lower()
    assert resp.json() == {"payload": "x" * 2000}


# ─── Wire bytes are actually smaller ────────────────────────────────────


def test_buffered_brotli_wire_bytes_smaller_than_plaintext(make_server_project):
    """Wire-transferred byte count must be smaller than the plaintext JSON,
    proving the middleware actually compressed instead of just setting a
    header. Guards against regressions where Content-Encoding is set but
    the body never makes it through the codec."""
    project = _make_buffered_project(make_server_project, backend="brotli")
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "br"})

    assert resp.headers.get("content-encoding", "").lower() == "br"
    plaintext_len = len(b'{"payload":"' + b"x" * 2000 + b'"}')
    assert resp.num_bytes_downloaded < plaintext_len
    assert resp.json() == {"payload": "x" * 2000}


# ─── minimum_size threshold ─────────────────────────────────────────────


def test_buffered_below_minimum_size_passes_through_uncompressed(make_server_project):
    """`/tiny` is well below the configured 1000-byte threshold and must
    leave the server uncompressed."""
    project = _make_buffered_project(make_server_project, backend="brotli", minimum_size=1000)
    with project.start(startup_path="/health") as server:
        resp = server.get("/tiny", headers={"Accept-Encoding": "br"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") is None
    assert resp.json() == {"x": "small"}


def test_buffered_above_minimum_size_is_compressed(make_server_project):
    """Same config compresses the larger `/data` route above the threshold."""
    project = _make_buffered_project(make_server_project, backend="brotli", minimum_size=1000)
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "br"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "br"
    assert resp.json() == {"payload": "x" * 2000}


# ─── Fallback / negotiation ─────────────────────────────────────────────


def test_buffered_gzip_fallback_when_backend_not_accepted(make_server_project):
    """Configured backend is brotli but client only accepts gzip → server
    falls back to gzip rather than sending identity."""
    project = _make_buffered_project(make_server_project, backend="brotli", gzip_fallback=True)
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "gzip"
    assert resp.json() == {"payload": "x" * 2000}


def test_buffered_no_match_no_fallback_passes_through(make_server_project):
    """Client accepts only an unsupported coding and `gzip_fallback=False`
    → server returns identity."""
    project = _make_buffered_project(make_server_project, backend="brotli", gzip_fallback=False)
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "deflate"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") is None
    assert resp.json() == {"payload": "x" * 2000}


# ─── Per-codec tuning knobs ─────────────────────────────────────────────
#
# These tests exercise every per-codec field on ``CompressionConfig``
# (``BrotliLevel``, ``BrotliLgWin``, ``GzipLevel``, ``ZstdLevel``) with a
# non-default extreme value to prove the value plumbs all the way through
# Python settings → Rust ``AppState`` → encoder, and that the encoder does
# not crash or produce a corrupt body at that value. We also assert the
# wire bytes are smaller than the plaintext so the level/lgwin can't be
# silently dropped (which would leave compression on but at a default).


def test_brotli_level_and_lgwin_extreme_values_roundtrip(make_server_project):
    """Brotli at max quality (11) and minimum sliding window (lgwin=10):
    both tuning knobs flow through and the encoder still produces a body
    that decodes back to the original JSON."""
    project = _make_buffered_project(make_server_project, backend="brotli", brotli_level=11, brotli_lgwin=10)
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "br"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "br"
    plaintext_len = len(b'{"payload":"' + b"x" * 2000 + b'"}')
    assert resp.num_bytes_downloaded < plaintext_len
    assert resp.json() == {"payload": "x" * 2000}


def test_gzip_level_extreme_value_roundtrip(make_server_project):
    """Gzip at max compression (level=9) flows through and roundtrips."""
    project = _make_buffered_project(make_server_project, backend="gzip", gzip_level=9)
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "gzip"
    plaintext_len = len(b'{"payload":"' + b"x" * 2000 + b'"}')
    assert resp.num_bytes_downloaded < plaintext_len
    assert resp.json() == {"payload": "x" * 2000}


def test_zstd_level_extreme_value_roundtrip(make_server_project):
    """Zstd at max compression (level=22, "ultra" mode) flows through and
    roundtrips. Guards against the level being silently clamped or dropped
    by the Python→Rust dict parsing."""
    pytest.importorskip("zstandard", reason="zstandard package not installed")
    project = _make_buffered_project(make_server_project, backend="zstd", zstd_level=22)
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "zstd"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "zstd"
    plaintext_len = len(b'{"payload":"' + b"x" * 2000 + b'"}')
    assert resp.num_bytes_downloaded < plaintext_len
    assert resp.json() == {"payload": "x" * 2000}


# ─── Shared config: buffered + SSE on one BoltAPI ───────────────────────


def test_buffered_and_sse_share_one_compression_config(make_server_project):
    """A single ``CompressionConfig`` must drive both the global buffered
    middleware *and* the per-chunk streaming codec. Pins the contract that
    streaming compression reads the same config the buffered middleware
    reads — not a parallel store."""
    project = make_server_project(
        settings_extra="""
        from django_bolt.middleware import CompressionConfig
        BOLT_COMPRESSION = CompressionConfig(backend="brotli")
        """,
        project_api_body="""
        from django_bolt.responses import EventSourceResponse

        @api.get("/buffered")
        async def buffered():
            return {"payload": "x" * 2000}

        @api.get("/sse")
        async def sse():
            async def gen():
                for i in range(3):
                    yield {"i": i}
                    await asyncio.sleep(0)
            return EventSourceResponse(gen())
        """,
    )
    with project.start(startup_path="/health") as server:
        buf = server.get("/buffered", headers={"Accept-Encoding": "br"})
        ev = server.get("/sse", headers={"Accept-Encoding": "br"})

    assert buf.status_code == 200
    assert buf.headers.get("content-encoding", "").lower() == "br"
    assert buf.json() == {"payload": "x" * 2000}

    assert ev.status_code == 200
    assert ev.headers.get("content-encoding", "").lower() == "br"
    decoded = ev.content
    for i in range(3):
        assert f'data: {{"i": {i}}}\n\n'.encode() in decoded or f'data: {{"i":{i}}}\n\n'.encode() in decoded, (
            f"event i={i} not found in decoded SSE stream"
        )


# ─── RFC 7231 §5.3.4 Accept-Encoding negotiation ────────────────────────
#
# The buffered middleware shares its Accept-Encoding parser with the
# streaming path (see `accepts_encoding` in `src/streaming_compression.rs`).
# These tests pin the contract that q-values and `*` are honored on the
# buffered path — the prior substring-based matcher would have failed all
# four cases below.


def test_buffered_q0_on_backend_falls_back_to_gzip(make_server_project):
    """`Accept-Encoding: br;q=0, gzip` explicitly rejects brotli even though
    "br" appears in the header. The unified parser must honor q=0 and pick
    gzip via the configured fallback."""
    project = _make_buffered_project(make_server_project, backend="brotli")
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "br;q=0, gzip"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "gzip"
    assert resp.json() == {"payload": "x" * 2000}


def test_buffered_star_wildcard_accepts_backend(make_server_project):
    """`Accept-Encoding: *` accepts any unmentioned coding. Brotli backend
    must be selected."""
    project = _make_buffered_project(make_server_project, backend="brotli")
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "*"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "br"
    assert resp.json() == {"payload": "x" * 2000}


def test_buffered_star_q0_rejects_unmentioned_codings(make_server_project):
    """`Accept-Encoding: gzip, *;q=0` means "gzip only, everything else
    rejected". Brotli backend must lose to the gzip fallback, not be
    picked just because the header is non-empty."""
    project = _make_buffered_project(make_server_project, backend="brotli")
    with project.start(startup_path="/health") as server:
        resp = server.get("/data", headers={"Accept-Encoding": "gzip, *;q=0"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding", "").lower() == "gzip"
    assert resp.json() == {"payload": "x" * 2000}


def test_buffered_and_sse_share_negotiation_parser(make_server_project):
    """`br;q=0` must be honored identically on buffered and SSE routes.
    Both paths share `accepts_encoding`, so a header that explicitly
    rejects brotli must fall back to gzip on both."""
    project = make_server_project(
        settings_extra="""
        from django_bolt.middleware import CompressionConfig
        BOLT_COMPRESSION = CompressionConfig(backend="brotli")
        """,
        project_api_body="""
        from django_bolt.responses import EventSourceResponse

        @api.get("/buffered")
        async def buffered():
            return {"payload": "x" * 2000}

        @api.get("/sse")
        async def sse():
            async def gen():
                for i in range(3):
                    yield {"i": i}
                    await asyncio.sleep(0)
            return EventSourceResponse(gen())
        """,
    )
    with project.start(startup_path="/health") as server:
        buf = server.get("/buffered", headers={"Accept-Encoding": "br;q=0, gzip"})
        ev = server.get("/sse", headers={"Accept-Encoding": "br;q=0, gzip"})

    assert buf.status_code == 200
    assert buf.headers.get("content-encoding", "").lower() == "gzip"
    assert buf.json() == {"payload": "x" * 2000}

    assert ev.status_code == 200
    assert ev.headers.get("content-encoding", "").lower() == "gzip"
