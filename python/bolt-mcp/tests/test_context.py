"""Context injection: progress/log/read_resource, schema exclusion, sample mechanism."""

from __future__ import annotations

import asyncio

import msgspec
from _helpers import _parse_all_sse, initialize, mcp_headers
from bolt_mcp import MCP, Context, mount_mcp
from bolt_mcp.context import Context as CtxClass
from bolt_mcp.sessions import Session, SessionManager

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


def _build(**mcp_kwargs):
    api = BoltAPI()
    mcp = MCP("ctx-server", **mcp_kwargs)

    @mcp.resource("config://x", mime_type="application/json")
    async def cfg() -> str:
        return '{"k":"v"}'

    @mcp.tool
    async def work(n: int, ctx: Context) -> dict:
        """Reports progress, logs, and reads a server resource."""
        for i in range(n):
            await ctx.report_progress(i + 1, n, message=f"{i + 1}/{n}")
        await ctx.info("almost done")
        data = await ctx.read_resource("config://x")
        return {"n": n, "cfg": data}

    mount_mcp(api, mcp)
    return api


def _call_work(client, session_id, *, n=2, token="tk"):
    params = {"name": "work", "arguments": {"n": n}}
    if token is not None:
        params["_meta"] = {"progressToken": token}
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": params}
    return client.post("/mcp", content=msgspec.json.encode(body), headers=mcp_headers(session_id=session_id))


def test_context_streams_progress_log_and_reads_resource():
    api = _build()
    with TestClient(api) as client:
        _, sid = initialize(client)
        resp = _call_work(client, sid, n=2)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events = _parse_all_sse(resp.content)
        progress = [e for e in events if e.get("method") == "notifications/progress"]
        logs = [e for e in events if e.get("method") == "notifications/message"]
        results = [e for e in events if "result" in e]

        assert [p["params"]["progress"] for p in progress] == [1, 2]
        assert all(p["params"]["progressToken"] == "tk" for p in progress)
        assert logs[0]["params"]["data"] == "almost done"
        assert results[0]["result"]["structuredContent"] == {"n": 2, "cfg": '{"k":"v"}'}


def test_ctx_param_excluded_from_input_schema():
    api = _build()
    with TestClient(api) as client:
        _, sid = initialize(client)
        resp = client.post(
            "/mcp",
            content=msgspec.json.encode({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            headers=mcp_headers(session_id=sid),
        )
        tools = {t["name"]: t for t in _parse_all_sse(resp.content)[0]["result"]["tools"]}
        props = tools["work"]["inputSchema"]["properties"]
        assert set(props) == {"n"}  # ctx is injected, not a client-supplied argument


def test_progress_dropped_without_progress_token():
    api = _build()
    with TestClient(api) as client:
        _, sid = initialize(client)
        resp = _call_work(client, sid, n=2, token=None)
        events = _parse_all_sse(resp.content)
        assert [e for e in events if e.get("method") == "notifications/progress"] == []
        results = [e for e in events if "result" in e]
        assert results[0]["result"]["structuredContent"] == {"n": 2, "cfg": '{"k":"v"}'}


def test_json_response_drops_progress_keeps_result():
    api = _build(json_response=True)
    with TestClient(api) as client:
        _, sid = initialize(client)
        resp = _call_work(client, sid, n=2)
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json()["result"]["structuredContent"] == {"n": 2, "cfg": '{"k":"v"}'}


def test_sample_in_stateless_is_in_band_error():
    api = BoltAPI()
    mcp = MCP("stateless-ctx", stateless=True)

    @mcp.tool
    async def ask(ctx: Context) -> dict:
        reply = await ctx.sample("hi")  # no session → must raise, surfaced in-band
        return {"reply": reply}

    mount_mcp(api, mcp)
    with TestClient(api) as client:
        initialize(client)
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ask", "arguments": {}}}
        resp = client.post("/mcp", content=msgspec.json.encode(body), headers=mcp_headers())
        results = [e for e in _parse_all_sse(resp.content) if "result" in e]
        assert results[0]["result"]["isError"] is True


def test_sample_without_client_capability_is_clear_in_band_error():
    # Stateful server, but the client did not advertise "sampling" at initialize →
    # the tool gets a clear in-band error up front (no cryptic -32601 round-trip).
    api = BoltAPI()
    mcp = MCP("cap-check")  # stateful

    @mcp.tool
    async def ask(ctx: Context) -> dict:
        return {"reply": await ctx.sample("hi")}

    mount_mcp(api, mcp)
    with TestClient(api) as client:
        _, sid = initialize(client)  # initialize sends capabilities={} → no sampling
        body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "ask", "arguments": {}}}
        resp = client.post("/mcp", content=msgspec.json.encode(body), headers=mcp_headers(session_id=sid))
        result = [e for e in _parse_all_sse(resp.content) if "result" in e][0]["result"]
        assert result["isError"] is True
        assert "sampling" in result["content"][0]["text"]


def test_context_sample_mechanism():
    """Unit-level: sample emits a server→client request and resolves on the reply."""

    async def scenario() -> None:
        session = Session(id="s1", client_capabilities={"sampling": {}})
        outgoing: asyncio.Queue = asyncio.Queue()
        ctx = CtxClass(mcp=None, session=session, request_id=1, outgoing=outgoing)

        task = asyncio.create_task(ctx.sample("summarize this", max_tokens=100))
        kind, req_id, method, params = await asyncio.wait_for(outgoing.get(), 1.0)
        assert kind == "request"
        assert method == "sampling/createMessage"
        assert params["maxTokens"] == 100
        assert params["messages"][0]["content"]["text"] == "summarize this"
        assert req_id in session.pending

        # Simulate the transport routing the client's response to the pending future.
        session.pending[req_id].set_result({"role": "assistant", "content": {"type": "text", "text": "ok"}})
        result = await asyncio.wait_for(task, 1.0)
        assert result["content"]["text"] == "ok"

    asyncio.run(scenario())


def test_stream_call_aclose_does_not_hang_on_blocked_tool():
    """Client disconnect (aclose) must cancel a tool still awaiting a sample reply."""

    async def scenario() -> None:
        mcp = MCP("disconnect")  # stateful → sample is allowed

        @mcp.tool
        async def ask(ctx: Context) -> dict:
            return {"reply": await ctx.sample("hi")}

        session = mcp.sessions.create()
        session.client_capabilities = {"sampling": {}}
        gen = mcp.stream_call({"name": "ask", "arguments": {}}, request=None, session=session, request_id=1)

        # The tool's ctx.sample emits a server→client request, then blocks on the reply.
        item = await asyncio.wait_for(gen.__anext__(), 1.0)
        assert item[0] == "request"

        # The reply never comes; aclose must return promptly instead of awaiting forever.
        await asyncio.wait_for(gen.aclose(), 1.0)

    asyncio.run(scenario())


def test_terminate_cancels_pending_requests():
    """Terminating a session unblocks tools awaiting a sample/elicit reply."""

    async def scenario() -> None:
        manager = SessionManager()
        session = manager.create()
        future = asyncio.get_running_loop().create_future()
        session.pending[1] = future

        assert manager.terminate(session.id) is True
        assert future.cancelled()

    asyncio.run(scenario())
