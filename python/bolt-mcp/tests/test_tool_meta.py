"""tools/list — a tool's ``_meta``."""

from __future__ import annotations

from _helpers import initialize, parse_rpc, post_rpc
from bolt_mcp import MCP, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

APP_META = {"ui": {"resourceUri": "ui://weather/view.html", "visibility": ["model", "app"]}}


def _server():
    api = BoltAPI()
    mcp = MCP("meta-test")

    @mcp.tool(meta=APP_META)
    def forecast(city: str) -> dict:
        return {"city": city}

    @mcp.tool
    def plain() -> str:
        return "ok"

    mount_mcp(api, mcp, path="/mcp")
    return api


def _tools(client) -> dict[str, dict]:
    _, session_id = initialize(client)
    resp = post_rpc(client, "tools/list", {}, session_id=session_id)
    return {t["name"]: t for t in parse_rpc(resp)["result"]["tools"]}


def test_tool_meta_is_listed_as_given():
    with TestClient(_server()) as client:
        assert _tools(client)["forecast"]["_meta"] == APP_META


def test_tool_without_meta_has_no_meta_key():
    with TestClient(_server()) as client:
        assert "_meta" not in _tools(client)["plain"]
