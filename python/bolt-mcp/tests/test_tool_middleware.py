"""tools/call — tool middleware with the request ``_meta``."""

from __future__ import annotations

import pytest
from _helpers import initialize, parse_rpc, post_rpc, post_rpc_modern
from bolt_mcp import MCP, Context, ToolCall, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _server(*middleware):
    api = BoltAPI()
    mcp = MCP("middleware-test")
    for fn in middleware:
        mcp.tool_middleware(fn)

    @mcp.tool
    def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    @mcp.tool
    async def progress(ctx: Context) -> str:
        await ctx.report_progress(1, 1)
        return "done"

    @mcp.tool
    def boom() -> dict:
        raise ValueError("kaboom")

    mount_mcp(api, mcp, path="/mcp")
    return api


def _legacy_call(client, name: str, arguments: dict, meta: dict | None = None) -> dict:
    _, session_id = initialize(client)
    params: dict = {"name": name, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    return parse_rpc(post_rpc(client, "tools/call", params, session_id=session_id))["result"]


def _recorder():
    calls: list[ToolCall] = []
    results: list[dict] = []

    async def record(call: ToolCall, call_next) -> dict:
        calls.append(call)
        result = await call_next()
        results.append(result)
        return result

    return record, calls, results


def test_middleware_gets_the_call_and_the_request_meta_for_a_tool_without_context():
    record, calls, results = _recorder()
    with TestClient(_server(record)) as client:
        result = _legacy_call(client, "add", {"a": 2, "b": 3}, meta={"traceparent": TRACEPARENT})
    assert result["structuredContent"] == {"sum": 5}
    (call,) = calls
    assert call.name == "add"
    assert call.arguments == {"a": 2, "b": 3}
    assert call.meta["traceparent"] == TRACEPARENT
    assert call.principal == {}
    assert results[0]["structuredContent"] == {"sum": 5}


def test_middleware_gets_the_meta_of_a_2026_07_28_request():
    record, calls, _ = _recorder()
    with TestClient(_server(record)) as client:
        resp = post_rpc_modern(client, "tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}})
        assert parse_rpc(resp)["result"]["structuredContent"] == {"sum": 2}
    assert calls[0].meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"


def test_tool_with_context_still_gets_its_context():
    record, calls, _ = _recorder()
    with TestClient(_server(record)) as client:
        result = _legacy_call(client, "progress", {})
    assert result["content"][0]["text"] == "done"
    assert calls[0].name == "progress"


def test_middleware_sees_tool_errors_and_argument_errors_as_results():
    record, _, results = _recorder()
    with TestClient(_server(record)) as client:
        _legacy_call(client, "boom", {})
        _legacy_call(client, "add", {"a": "x", "b": 1})
    assert [r["isError"] for r in results] == [True, True]
    assert results[1]["content"][0]["text"].startswith("Invalid arguments")


def test_first_registered_middleware_runs_outermost_and_can_change_the_result():
    order: list[str] = []

    async def outer(call: ToolCall, call_next) -> dict:
        order.append("outer in")
        result = await call_next()
        order.append("outer out")
        return {**result, "_meta": {"seen-by": "outer"}}

    async def inner(call: ToolCall, call_next) -> dict:
        order.append("inner in")
        result = await call_next()
        order.append("inner out")
        return result

    with TestClient(_server(outer, inner)) as client:
        result = _legacy_call(client, "add", {"a": 2, "b": 2})
    assert order == ["outer in", "inner in", "inner out", "outer out"]
    assert result["_meta"] == {"seen-by": "outer"}


def test_sync_middleware_is_rejected():
    mcp = MCP("sync-middleware-test")
    with pytest.raises(TypeError, match="must be an async function"):

        @mcp.tool_middleware
        def sync_middleware(call, call_next):
            return call_next()


def test_input_request_passes_through_middleware():
    record, calls, results = _recorder()
    api = BoltAPI()
    mcp = MCP("middleware-mrtr-test")
    mcp.tool_middleware(record)

    @mcp.tool
    async def confirm(item: str, ctx: Context) -> dict:
        return {"answer": await ctx.elicit(f"Really touch {item}?")}

    mount_mcp(api, mcp)
    with TestClient(api) as client:
        resp = post_rpc_modern(
            client,
            "tools/call",
            {"name": "confirm", "arguments": {"item": "db"}},
            capabilities={"elicitation": {}},
        )
        result = parse_rpc(resp)["result"]
    assert result["resultType"] == "input_required"
    assert [c.name for c in calls] == ["confirm"]
    assert results == []
