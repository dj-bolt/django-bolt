"""A Context-taking tool streams progress/log notifications, then returns its result."""

from __future__ import annotations

import msgspec
import pytest
from _helpers import _parse_all_sse, initialize, mcp_headers
from bolt_mcp import MCP, Context, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


def _call_crunch(client, session_id):
    body = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "crunch", "arguments": {"n": 3}, "_meta": {"progressToken": "tok1"}},
    }
    return client.post("/mcp", content=msgspec.json.encode(body), headers=mcp_headers(session_id=session_id))


def _build(**mcp_kwargs):
    api = BoltAPI()
    mcp = MCP("stream-server", **mcp_kwargs)

    @mcp.tool
    async def crunch(n: int, ctx: Context) -> dict:
        for i in range(n):
            await ctx.report_progress(i, n, message=f"step {i}")
        await ctx.info("almost done")
        return {"done": n}

    mount_mcp(api, mcp)
    return api


def test_context_tool_streams_progress_then_result():
    api = _build()
    with TestClient(api) as client:
        _, sid = initialize(client)
        resp = _call_crunch(client, sid)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events = _parse_all_sse(resp.content)
        progress = [e for e in events if e.get("method") == "notifications/progress"]
        logs = [e for e in events if e.get("method") == "notifications/message"]
        results = [e for e in events if "result" in e]

        assert len(progress) == 3
        assert [p["params"]["progress"] for p in progress] == [0, 1, 2]
        assert all(p["params"]["progressToken"] == "tok1" for p in progress)
        assert progress[0]["params"]["total"] == 3

        assert len(logs) == 1
        assert logs[0]["params"]["data"] == "almost done"

        # Exactly one final result, carrying the request id and the returned value.
        assert len(results) == 1
        assert results[0]["id"] == 5
        assert results[0]["result"]["isError"] is False
        assert results[0]["result"]["structuredContent"] == {"done": 3}


def test_progress_omitted_without_progress_token():
    api = _build()
    with TestClient(api) as client:
        _, sid = initialize(client)
        body = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "crunch", "arguments": {"n": 2}},  # no _meta.progressToken
        }
        resp = client.post("/mcp", content=msgspec.json.encode(body), headers=mcp_headers(session_id=sid))
        events = _parse_all_sse(resp.content)
        assert [e for e in events if e.get("method") == "notifications/progress"] == []
        results = [e for e in events if "result" in e]
        assert results[0]["result"]["structuredContent"] == {"done": 2}


def test_context_tool_json_response_returns_final_result_only():
    # json_response mode can't stream: the Context drops notifications (outgoing=None)
    # and the tool's return value is sent as the single result.
    api = _build(json_response=True)
    with TestClient(api) as client:
        _, sid = initialize(client)
        resp = _call_crunch(client, sid)
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert body["result"]["structuredContent"] == {"done": 3}


def test_async_generator_tool_is_rejected_at_registration():
    # The generator-yield streaming style was removed in favor of the Context API.
    mcp = MCP("nogen")
    with pytest.raises(TypeError, match="async generator"):

        @mcp.tool
        async def gen(n: int):
            yield {"n": n}
