"""HTTP-level content negotiation and parse errors on POST /mcp."""

from __future__ import annotations

from _helpers import INITIALIZE_PARAMS, make_server, mcp_headers, post_rpc, rpc_body

from django_bolt.testing import TestClient

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602


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


def test_malformed_body_rejected():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = client.post("/mcp", content=b"{not json", headers=mcp_headers())
        # The spec requires an HTTP error status for an unparseable message;
        # the exact code is transport-defined (pre-0.2: 400, rmcp: 415).
        assert 400 <= resp.status_code < 500


def test_jsonrpc_batch_array_rejected():
    api, _ = make_server()
    with TestClient(api) as client:
        batch = b"[" + rpc_body("ping", id=1) + b"," + rpc_body("ping", id=2) + b"]"
        resp = client.post("/mcp", content=batch, headers=mcp_headers())
        # Batching was removed in the 2025-06-18 revision; the body must be a
        # single message, so an array is rejected with an HTTP error status.
        assert 400 <= resp.status_code < 500
