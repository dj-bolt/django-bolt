"""HTTP-level content negotiation and parse errors on POST /mcp."""

from __future__ import annotations

from _helpers import INITIALIZE_PARAMS, make_server, mcp_headers, post_rpc, rpc_body
from bolt_mcp.transport import _accepts
from bolt_mcp.types import INVALID_PARAMS, INVALID_REQUEST, PARSE_ERROR

from django_bolt.testing import TestClient


def test_accept_header_honors_wildcards():
    # Regression: a GET/POST with `Accept: */*` (curl/browser default) or `text/*`
    # must be treated as accepting text/event-stream — not rejected with 406.
    assert _accepts("*/*", "text/event-stream") is True
    assert _accepts("text/*", "text/event-stream") is True
    assert _accepts("application/json, text/event-stream", "text/event-stream") is True
    assert _accepts("", "text/event-stream") is True  # absent Accept = accept all
    assert _accepts("*/*", "application/json") is True
    # A concrete, non-matching type is still rejected.
    assert _accepts("application/json", "text/event-stream") is False


def test_missing_event_stream_accept_returns_406():
    api, _ = make_server()
    with TestClient(api) as client:
        # Non-JSON (default) mode requires the client to accept text/event-stream too.
        resp = post_rpc(client, "initialize", INITIALIZE_PARAMS, accept="application/json")
        assert resp.status_code == 406


def test_non_json_content_type_returns_415():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = post_rpc(client, "initialize", INITIALIZE_PARAMS, content_type="text/plain")
        assert resp.status_code == 415


def test_malformed_body_returns_400_parse_error():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = client.post("/mcp", content=b"{not json", headers=mcp_headers())
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == PARSE_ERROR


def test_jsonrpc_batch_array_rejected():
    api, _ = make_server()
    with TestClient(api) as client:
        batch = b"[" + rpc_body("ping", id=1) + b"," + rpc_body("ping", id=2) + b"]"
        resp = client.post("/mcp", content=batch, headers=mcp_headers())
        assert resp.status_code == 400
        # Batching was removed in the 2025-06-18 revision; the exact code is
        # impl-defined (the SDK yields INVALID_PARAMS, INVALID_REQUEST is also valid).
        assert resp.json()["error"]["code"] in (INVALID_REQUEST, INVALID_PARAMS)
