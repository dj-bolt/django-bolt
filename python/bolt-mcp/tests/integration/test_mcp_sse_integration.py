"""Live Streamable HTTP SSE behavior over a real runbolt server.

Covers the parts the buffered in-process TestClient cannot: the POST→SSE
response framing, the long-lived GET listen channel, single-stream-per-session
(409), and DELETE terminating the session.
"""

from __future__ import annotations

import pytest
from _helpers import INITIALIZE_PARAMS, mcp_headers, rpc_body

pytestmark = pytest.mark.server_integration

MCP_API_BODY = """
from bolt_mcp import MCP, mount_mcp

mcp = MCP("sse-itest", "1.0")


# Default (non json_response) servers stream every POST response as SSE.
@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}


mount_mcp(api, mcp)
"""


def _initialize(server) -> str:
    resp = server.client.post(
        server.url("/mcp"),
        content=rpc_body("initialize", INITIALIZE_PARAMS),
        headers=mcp_headers(),
    )
    assert resp.status_code == 200
    return resp.headers["mcp-session-id"]


def test_tool_call_streams_sse_response(make_server_project):
    project = make_server_project(project_api_body=MCP_API_BODY)
    with project.start() as server:
        session_id = _initialize(server)
        with server.client.stream(
            "POST",
            server.url("/mcp"),
            content=rpc_body("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}),
            headers=mcp_headers(session_id=session_id),
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            data_lines = [line for line in resp.iter_lines() if line.startswith("data:")]
        assert any('"sum": 5' in line or '"sum":5' in line for line in data_lines)


def test_get_listen_channel_single_stream_and_delete(make_server_project):
    project = make_server_project(project_api_body=MCP_API_BODY)
    with project.start() as server:
        session_id = _initialize(server)
        sse_headers = {"Accept": "text/event-stream", "Mcp-Session-Id": session_id}

        with server.client.stream("GET", server.url("/mcp"), headers=sse_headers) as listen:
            assert listen.status_code == 200
            assert listen.headers["content-type"].startswith("text/event-stream")

            # A second concurrent listen stream for the same session is rejected.
            second = server.client.get(server.url("/mcp"), headers=sse_headers)
            assert second.status_code == 409

            # Terminating the session is accepted and unblocks the listener.
            deleted = server.client.request("DELETE", server.url("/mcp"), headers={"Mcp-Session-Id": session_id})
            assert deleted.status_code == 200
