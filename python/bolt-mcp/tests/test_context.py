"""Context behavior through the full stack: schema exclusion, progress,
read_resource, and the elicit/sample capability errors.

MRTR (modern-era elicitation) round-trips live in test_mrtr.py; modern-era
log gating lives in test_modern_logging.py.
"""

from __future__ import annotations

import msgspec
from _helpers import _parse_all_sse, initialize, mcp_headers, parse_rpc, post_rpc_modern
from bolt_mcp import MCP, Context, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


def _build(**mcp_kwargs):
    api = BoltAPI()
    mcp = MCP("ctx-server", **mcp_kwargs)

    @mcp.resource("data://greeting")
    def greeting() -> str:
        return "hi from resource"

    @mcp.tool
    async def uses_resource(ctx: Context) -> dict:
        text = await ctx.read_resource("data://greeting")
        return {"resource": text}

    @mcp.tool
    async def wants_progress(n: int, ctx: Context) -> dict:
        for i in range(n):
            await ctx.report_progress(i, n)
        return {"done": n}

    @mcp.tool
    async def asks_user(ctx: Context) -> dict:
        answer = await ctx.elicit("Proceed?")
        return {"answer": answer}

    @mcp.tool
    async def asks_llm(ctx: Context) -> dict:
        return {"reply": await ctx.sample("hello")}

    mount_mcp(api, mcp)
    return api, mcp


def _legacy_call(client, sid, name, arguments=None, *, meta=None, id=7):
    params = {"name": name, "arguments": arguments or {}}
    if meta is not None:
        params["_meta"] = meta
    body = {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": params}
    return client.post("/mcp", content=msgspec.json.encode(body), headers=mcp_headers(session_id=sid))


def test_ctx_param_excluded_from_input_schema():
    _, mcp = _build()
    schema = mcp._tools["wants_progress"].input_schema
    assert set(schema["properties"]) == {"n"}


def test_context_boolean_progress_property_is_explicitly_named():
    rust_ctx = type("RustContext", (), {"has_progress_token": True})()
    ctx = Context(rust_ctx)
    assert ctx.has_progress_token is True
    assert not hasattr(ctx, "progress_token")


def test_context_read_resource_resolves_own_resources():
    api, _ = _build()
    with TestClient(api) as client:
        resp = post_rpc_modern(client, "tools/call", {"name": "uses_resource", "arguments": {}})
        body = parse_rpc(resp)
        assert body["result"]["structuredContent"] == {"resource": "hi from resource"}


def test_progress_streams_with_token_legacy_session():
    api, _ = _build()
    with TestClient(api) as client:
        _, sid = initialize(client)
        resp = _legacy_call(client, sid, "wants_progress", {"n": 2}, meta={"progressToken": "t1"})
        events = _parse_all_sse(resp.content)
        progress = [e for e in events if e.get("method") == "notifications/progress"]
        assert [p["params"]["progress"] for p in progress] == [0, 1]


def test_progress_streams_with_token_modern():
    api, _ = _build()
    with TestClient(api) as client:
        resp = post_rpc_modern(
            client, "tools/call", {"name": "wants_progress", "arguments": {"n": 2}}, progress_token="t2"
        )
        events = _parse_all_sse(resp.content)
        progress = [e for e in events if e.get("method") == "notifications/progress"]
        assert [p["params"]["progress"] for p in progress] == [0, 1]
        assert all(p["params"]["progressToken"] == "t2" for p in progress)


def test_progress_dropped_without_progress_token():
    api, _ = _build()
    with TestClient(api) as client:
        resp = post_rpc_modern(client, "tools/call", {"name": "wants_progress", "arguments": {"n": 2}})
        if "event-stream" in resp.headers.get("content-type", ""):
            events = _parse_all_sse(resp.content)
            assert [e for e in events if e.get("method") == "notifications/progress"] == []
        assert parse_rpc(resp)["result"]["structuredContent"] == {"done": 2}


def test_elicit_without_client_capability_is_clear_in_band_error_legacy():
    # Legacy peer that did NOT advertise elicitation: the live path must fail
    # with an actionable in-band error, not a hang or an opaque protocol error.
    api, _ = _build()
    with TestClient(api) as client:
        _, sid = initialize(client)  # INITIALIZE_PARAMS advertises no capabilities
        resp = _legacy_call(client, sid, "asks_user")
        result = parse_rpc(resp)["result"]
        assert result["isError"] is True
        assert "elicitation" in result["content"][0]["text"]


def test_sample_without_client_capability_is_clear_in_band_error_legacy():
    api, _ = _build()
    with TestClient(api) as client:
        _, sid = initialize(client)
        resp = _legacy_call(client, sid, "asks_llm")
        result = parse_rpc(resp)["result"]
        assert result["isError"] is True
        assert "sampling" in result["content"][0]["text"]


def test_sample_on_modern_peer_is_clear_in_band_error():
    # Sampling is deprecated in 2026-07-28 (SEP-2577); the modern path refuses
    # it with a message pointing at provider APIs — even when the client
    # advertises the capability.
    api, _ = _build()
    with TestClient(api) as client:
        resp = post_rpc_modern(
            client,
            "tools/call",
            {"name": "asks_llm", "arguments": {}},
            capabilities={"sampling": {}},
        )
        result = parse_rpc(resp)["result"]
        assert result["isError"] is True
        assert "sampling" in result["content"][0]["text"].lower()
