"""Auto-expose existing django-bolt endpoints as MCP tools."""

from __future__ import annotations

from _helpers import initialize, parse_rpc, post_rpc
from bolt_mcp import MCP, expose_as_tool, expose_routes, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


def _build():
    api = BoltAPI()
    mcp = MCP("autoexpose-server")

    @api.get("/items/{item_id}")
    @expose_as_tool(name="get_item", description="Fetch an item by id")
    async def get_item(item_id: int) -> dict:
        return {"id": item_id, "name": f"item-{item_id}"}

    expose_routes(mcp, api)
    mount_mcp(api, mcp)
    return api, mcp


def test_marked_endpoint_appears_in_tools_list():
    api, _ = _build()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "tools/list", session_id=session_id)
        tools = {t["name"]: t for t in parse_rpc(resp)["result"]["tools"]}
        assert "get_item" in tools
        schema = tools["get_item"]["inputSchema"]
        assert schema["properties"]["item_id"]["type"] == "integer"
        assert schema["required"] == ["item_id"]


def test_calling_exposed_endpoint_binds_path_param():
    api, _ = _build()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(
            client,
            "tools/call",
            {"name": "get_item", "arguments": {"item_id": 5}},
            session_id=session_id,
        )
        assert resp.status_code == 200
        result = parse_rpc(resp)["result"]
        assert result["isError"] is False
        assert result["structuredContent"] == {"id": 5, "name": "item-5"}
