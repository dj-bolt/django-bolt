"""Bidirectional server→client requests (sample/elicit) over a real runbolt server.

Proves the full round-trip the buffered in-process TestClient can't: the tool sends a
request to the client *mid-execution* on the POST SSE stream, the client replies on a
separate POST, and the tool resumes and returns a result built from that reply.
"""

from __future__ import annotations

import json

import httpx
import msgspec
import pytest
from _helpers import PROTOCOL, mcp_headers, rpc_body

pytestmark = pytest.mark.server_integration

MCP_API_BODY = """
from bolt_mcp import MCP, Context, mount_mcp

mcp = MCP("bidi-itest", "1.0")   # stateful: sample/elicit need a session


@mcp.tool
async def echo_via_llm(text: str, ctx: Context) -> dict:
    reply = await ctx.sample(text)               # ask the client's LLM
    return {"reply": reply["content"]["text"]}


@mcp.tool
async def confirm(ctx: Context) -> dict:
    answer = await ctx.elicit("Proceed?")        # ask the user
    return {"answer": answer["content"]}


mount_mcp(api, mcp)
"""


def _initialize(server):
    resp = server.client.post(
        server.url("/mcp"),
        content=rpc_body(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {"sampling": {}, "elicitation": {}},
                "clientInfo": {"name": "it", "version": "1"},
            },
        ),
        headers=mcp_headers(protocol=None),
    )
    assert resp.status_code == 200
    return resp.headers["mcp-session-id"]


def _round_trip(server, session_id, *, tool, arguments, expect_method, client_result):
    """Call a bidirectional tool; answer the server's request; return the final result."""
    call = rpc_body("tools/call", {"name": tool, "arguments": arguments}, id=7)
    server_request_id = None
    final = None
    with server.client.stream(
        "POST", server.url("/mcp"), content=call, headers=mcp_headers(session_id=session_id)
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            msg = json.loads(line[len("data:") :].strip())
            if msg.get("method") == expect_method:
                server_request_id = msg["id"]
                # The client replies on a *separate* connection.
                reply = msgspec.json.encode({"jsonrpc": "2.0", "id": server_request_id, "result": client_result})
                ack = httpx.post(
                    server.url("/mcp"), content=reply, headers=mcp_headers(session_id=session_id), timeout=10.0
                )
                assert ack.status_code == 202
            elif "result" in msg:
                final = msg
                break
    assert server_request_id is not None, f"server never sent a {expect_method} request"
    return final


def test_sampling_round_trip(make_server_project):
    project = make_server_project(project_api_body=MCP_API_BODY)
    with project.start() as server:
        sid = _initialize(server)
        final = _round_trip(
            server,
            sid,
            tool="echo_via_llm",
            arguments={"text": "hello"},
            expect_method="sampling/createMessage",
            client_result={"role": "assistant", "content": {"type": "text", "text": "HELLO"}, "model": "test"},
        )
        assert final["result"]["structuredContent"] == {"reply": "HELLO"}


def test_elicitation_round_trip(make_server_project):
    project = make_server_project(project_api_body=MCP_API_BODY)
    with project.start() as server:
        sid = _initialize(server)
        final = _round_trip(
            server,
            sid,
            tool="confirm",
            arguments={},
            expect_method="elicitation/create",
            client_result={"action": "accept", "content": {"ok": True}},
        )
        assert final["result"]["structuredContent"] == {"answer": {"ok": True}}
