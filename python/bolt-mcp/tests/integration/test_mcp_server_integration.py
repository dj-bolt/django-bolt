"""End-to-end MCP handshake + tools over a real runbolt server (JSON path)."""

from __future__ import annotations

import msgspec
import pytest

pytestmark = pytest.mark.server_integration

DUAL_ACCEPT = "application/json, text/event-stream"

MCP_API_BODY = """
from bolt_mcp import MCP, mount_mcp

mcp = MCP("itest-server", "1.2.3")


@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}


mount_mcp(api, mcp)
"""


def _headers(session_id=None):
    h = {"Accept": DUAL_ACCEPT, "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def _post(server, method, params=None, *, id=1, session_id=None):
    body = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        body["id"] = id
    if params is not None:
        body["params"] = params
    return server.client.post(server.url("/mcp"), content=msgspec.json.encode(body), headers=_headers(session_id))


def _rpc(resp):
    """Decode a JSON-RPC message from a /mcp 200 response (SSE default or JSON mode)."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        text = resp.text.replace("\r\n", "\n")
        for block in text.split("\n\n"):
            data = [line[len("data:"):].lstrip() for line in block.split("\n") if line.startswith("data:")]
            if data:
                return msgspec.json.decode("\n".join(data).encode())
        raise AssertionError(f"no SSE data frame in: {text!r}")
    return msgspec.json.decode(resp.content)


def test_full_handshake_and_tool_call(make_server_project):
    project = make_server_project(project_api_body=MCP_API_BODY)
    with project.start() as server:
        init = _post(
            server,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "it", "version": "1"}},
        )
        assert init.status_code == 200
        session_id = init.headers.get("mcp-session-id")
        assert session_id
        assert _rpc(init)["result"]["serverInfo"]["name"] == "itest-server"

        notified = _post(server, "notifications/initialized", id=None, session_id=session_id)
        assert notified.status_code == 202

        listed = _post(server, "tools/list", session_id=session_id)
        names = {t["name"] for t in _rpc(listed)["result"]["tools"]}
        assert "add" in names

        called = _post(server, "tools/call", {"name": "add", "arguments": {"a": 4, "b": 5}}, session_id=session_id)
        assert _rpc(called)["result"]["structuredContent"] == {"sum": 9}
