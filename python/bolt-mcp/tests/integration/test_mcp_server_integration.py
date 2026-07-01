"""End-to-end MCP handshake + tools over a real runbolt server (JSON path)."""

from __future__ import annotations

import pytest
from _helpers import INITIALIZE_PARAMS, mcp_headers, parse_rpc, rpc_body

pytestmark = pytest.mark.server_integration

MCP_API_BODY = """
from bolt_mcp import MCP, mount_mcp

mcp = MCP("itest-server", "1.2.3")


@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}


mount_mcp(api, mcp)
"""


def _post(server, method, params=None, *, request_id=1, session_id=None):
    return server.client.post(
        server.url("/mcp"), content=rpc_body(method, params, id=request_id), headers=mcp_headers(session_id=session_id)
    )


def test_full_handshake_and_tool_call(make_server_project):
    project = make_server_project(project_api_body=MCP_API_BODY)
    with project.start() as server:
        init = _post(server, "initialize", INITIALIZE_PARAMS)
        assert init.status_code == 200
        session_id = init.headers.get("mcp-session-id")
        assert session_id
        assert parse_rpc(init)["result"]["serverInfo"]["name"] == "itest-server"

        notified = _post(server, "notifications/initialized", request_id=None, session_id=session_id)
        assert notified.status_code == 202

        listed = _post(server, "tools/list", session_id=session_id)
        names = {t["name"] for t in parse_rpc(listed)["result"]["tools"]}
        assert "add" in names

        called = _post(server, "tools/call", {"name": "add", "arguments": {"a": 4, "b": 5}}, session_id=session_id)
        assert parse_rpc(called)["result"]["structuredContent"] == {"sum": 9}
