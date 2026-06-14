"""Initialize handshake + session lifecycle + base JSON-RPC methods."""

from __future__ import annotations

from _helpers import initialize, make_server, parse_rpc, post_rpc
from bolt_mcp.types import METHOD_NOT_FOUND, SUPPORTED_PROTOCOL_VERSIONS

from django_bolt.testing import TestClient


def test_initialize_returns_session_and_capabilities():
    api, _ = make_server()
    with TestClient(api) as client:
        resp, session_id = initialize(client)
        assert resp.status_code == 200
        assert session_id, "initialize must return an Mcp-Session-Id header"
        body = parse_rpc(resp)
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        result = body["result"]
        assert result["protocolVersion"] in SUPPORTED_PROTOCOL_VERSIONS
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "test-server"
        assert result["serverInfo"]["version"] == "9.9.9"


def test_initialized_notification_returns_202_empty():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "notifications/initialized", id=None, session_id=session_id)
        assert resp.status_code == 202
        assert resp.content == b""


def test_ping_returns_empty_result():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "ping", session_id=session_id)
        assert resp.status_code == 200
        assert parse_rpc(resp)["result"] == {}


def test_unknown_session_returns_404():
    api, _ = make_server()
    with TestClient(api) as client:
        initialize(client)
        resp = post_rpc(client, "ping", session_id="does-not-exist")
        assert resp.status_code == 404


def test_missing_session_on_non_init_returns_400():
    api, _ = make_server()
    with TestClient(api) as client:
        initialize(client)
        resp = post_rpc(client, "ping", session_id=None)
        assert resp.status_code == 400


def test_unsupported_protocol_version_returns_400():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "ping", session_id=session_id, protocol="1999-01-01")
        assert resp.status_code == 400


def test_unknown_method_returns_jsonrpc_method_not_found():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "does/not/exist", session_id=session_id)
        assert resp.status_code == 200
        assert parse_rpc(resp)["error"]["code"] == METHOD_NOT_FOUND
