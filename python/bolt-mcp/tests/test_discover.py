"""server/discover + SEP-2549 caching hints, served from the Rust catalog."""

from __future__ import annotations

from _helpers import PROTOCOL_MODERN, make_server, parse_rpc, post_rpc_modern
from bolt_mcp import MCP, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


def test_discover_reports_versions_capabilities_and_identity():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = post_rpc_modern(client, "server/discover")
        assert resp.status_code == 200
        result = parse_rpc(resp)["result"]
        assert result["resultType"] == "complete"
        assert PROTOCOL_MODERN in result["supportedVersions"]
        assert "2025-06-18" in result["supportedVersions"]
        assert "tools" in result["capabilities"]
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "test-server"


def test_list_results_carry_ttl_and_cache_scope():
    api = BoltAPI()
    mcp = MCP("cached", list_ttl_ms=60000, list_cache_scope="public")

    @mcp.tool
    async def t() -> dict:
        return {}

    @mcp.resource("data://x")
    def x() -> str:
        return "x"

    @mcp.prompt
    def p() -> str:
        return "p"

    mount_mcp(api, mcp)
    with TestClient(api) as client:
        for method in ("tools/list", "resources/list", "prompts/list", "resources/templates/list"):
            result = parse_rpc(post_rpc_modern(client, method))["result"]
            assert result["ttlMs"] == 60000, method
            assert result["cacheScope"] == "public", method
        result = parse_rpc(post_rpc_modern(client, "server/discover"))["result"]
        assert result["ttlMs"] == 60000
        assert result["cacheScope"] == "public"
        result = parse_rpc(post_rpc_modern(client, "resources/read", {"uri": "data://x"}))["result"]
        assert result["ttlMs"] == 60000
        assert result["cacheScope"] == "public"


def test_default_caching_hints_are_uncacheable():
    api, _ = make_server()
    with TestClient(api) as client:
        result = parse_rpc(post_rpc_modern(client, "tools/list"))["result"]
        assert result["ttlMs"] == 0
        assert result["cacheScope"] == "private"


def test_invalid_cache_scope_rejected_at_registration():
    try:
        MCP("bad", list_cache_scope="everyone")
    except ValueError as exc:
        assert "public" in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid list_cache_scope")
