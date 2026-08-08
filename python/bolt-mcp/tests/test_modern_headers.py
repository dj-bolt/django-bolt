"""2026-07-28 request-metadata validation: routing headers, version errors,
unknown methods, discover, and Origin checking."""

from __future__ import annotations

import msgspec
from _helpers import make_server, parse_rpc, post_rpc_modern
from bolt_mcp import MCP, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022
METHOD_NOT_FOUND = -32601


def test_modern_tools_call_round_trip():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = post_rpc_modern(client, "tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}})
        assert resp.status_code == 200
        assert parse_rpc(resp)["result"]["structuredContent"] == {"sum": 5}


def test_results_carry_result_type_complete():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = post_rpc_modern(client, "tools/list")
        assert parse_rpc(resp)["result"]["resultType"] == "complete"


def test_mcp_name_header_mismatch_rejected_with_32020():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = post_rpc_modern(
            client,
            "tools/call",
            {"name": "add", "arguments": {"a": 1, "b": 1}},
            headers_override={"Mcp-Name": "not-add"},
        )
        assert resp.status_code == 400
        assert parse_rpc(resp)["error"]["code"] == HEADER_MISMATCH


def test_mcp_method_header_mismatch_rejected_with_32020():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = post_rpc_modern(
            client,
            "tools/list",
            headers_override={"Mcp-Method": "tools/call"},
        )
        assert resp.status_code == 400
        assert parse_rpc(resp)["error"]["code"] == HEADER_MISMATCH


def test_unsupported_protocol_version_returns_32022_with_supported_list():
    api, _ = make_server()
    with TestClient(api) as client:
        # Header and _meta must agree (else HeaderMismatch fires first); both
        # carry the unknown version to reach the version check itself.
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2099-01-01",
                    "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "1.0"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        }
        resp = client.post(
            "/mcp",
            content=msgspec.json.encode(body),
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "2099-01-01",
                "Mcp-Method": "tools/list",
            },
        )
        assert resp.status_code == 400
        error = parse_rpc(resp)["error"]
        assert error["code"] == UNSUPPORTED_PROTOCOL_VERSION
        assert "2026-07-28" in str(error.get("data", {}).get("supported", []))


def test_unknown_method_returns_404_with_method_not_found():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = post_rpc_modern(client, "does/not/exist")
        assert resp.status_code == 404
        assert parse_rpc(resp)["error"]["code"] == METHOD_NOT_FOUND


def test_origin_allowlist_rejects_foreign_origin():
    api = BoltAPI()
    mcp = MCP("origin-server")

    @mcp.tool
    async def t() -> dict:
        return {}

    mount_mcp(api, mcp, allowed_origins=["https://good.example"])
    with TestClient(api) as client:
        denied = post_rpc_modern(client, "tools/list", headers_override={"Origin": "https://evil.example"})
        assert denied.status_code == 403
        allowed = post_rpc_modern(client, "tools/list", headers_override={"Origin": "https://good.example"})
        assert allowed.status_code == 200


def test_host_allowlist_rejects_foreign_host():
    api = BoltAPI()
    mcp = MCP("host-server")

    @mcp.tool
    async def t() -> dict:
        return {}

    mount_mcp(api, mcp, allowed_hosts=["localhost", "127.0.0.1"])
    with TestClient(api) as client:
        # TestClient requests carry Host: testserver — not in the allowlist.
        resp = post_rpc_modern(client, "tools/list")
        assert resp.status_code == 403
