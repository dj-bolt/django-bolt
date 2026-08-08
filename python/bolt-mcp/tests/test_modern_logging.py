"""Per-request logging gate (SEP-2575): notifications/message only when the
request carried io.modelcontextprotocol/logLevel, filtered by severity."""

from __future__ import annotations

from _helpers import _parse_all_sse, parse_rpc, post_rpc_modern
from bolt_mcp import MCP, Context, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


def _build():
    api = BoltAPI()
    mcp = MCP("log-server")

    @mcp.tool
    async def chatty(ctx: Context) -> dict:
        await ctx.debug("dbg")
        await ctx.info("inf")
        await ctx.error("err")
        return {"ok": True}

    mount_mcp(api, mcp)
    return api


def _log_events(resp):
    if "event-stream" not in resp.headers.get("content-type", ""):
        return []
    return [e for e in _parse_all_sse(resp.content) if e.get("method") == "notifications/message"]


def test_no_log_level_means_no_messages():
    api = _build()
    with TestClient(api) as client:
        resp = post_rpc_modern(client, "tools/call", {"name": "chatty", "arguments": {}})
        assert _log_events(resp) == []
        assert parse_rpc(resp)["result"]["structuredContent"] == {"ok": True}


def test_log_level_debug_receives_everything():
    api = _build()
    with TestClient(api) as client:
        resp = post_rpc_modern(client, "tools/call", {"name": "chatty", "arguments": {}}, log_level="debug")
        levels = [e["params"]["level"] for e in _log_events(resp)]
        assert levels == ["debug", "info", "error"]


def test_log_level_filters_below_threshold():
    api = _build()
    with TestClient(api) as client:
        resp = post_rpc_modern(client, "tools/call", {"name": "chatty", "arguments": {}}, log_level="error")
        levels = [e["params"]["level"] for e in _log_events(resp)]
        assert levels == ["error"]
