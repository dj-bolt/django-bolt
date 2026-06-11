"""Every MCP feature this PR adds, exercised over a real runbolt server.

The in-process TestClient unit tests cover these method-by-method; this proves the same
surface works end to end against a live process and real TCP/SSE: the initialize
handshake + advertised capabilities, ``ping``, tools (native + a REST route exposed via
``expose=``), in-band tool errors, a Context tool streaming progress/log notifications
ahead of its result, static + templated resources, prompts, and JSON-RPC error framing.
Two extra single-server tests cover the ``json_response`` and ``stateless`` transport modes.

Most tests share one module-scoped server (``feature_server``) since they only issue
read-only MCP calls; the mode tests spin up their own server with a different ``MCP(...)``.
"""

from __future__ import annotations

import pytest
from _helpers import PROTOCOL, _parse_all_sse, mcp_headers, parse_rpc, rpc_body

pytestmark = pytest.mark.server_integration

# Picked up by the module-scoped ``feature_server`` fixture (see integration/conftest.py).
MCP_API_BODY = """
from bolt_mcp import MCP, Context, mount_mcp

mcp = MCP("features-itest", "2.0")  # default: stateful + SSE framing


@mcp.tool
async def add(a: int, b: int) -> dict:
    \"\"\"Add two integers.\"\"\"
    return {"sum": a + b}


@mcp.tool
async def boom() -> dict:
    \"\"\"Always raises — exercises in-band tool errors.\"\"\"
    raise ValueError("kaboom")


@mcp.tool
async def crunch(n: int, ctx: Context) -> dict:
    \"\"\"Stream progress, then return — exercises Context notifications over SSE.\"\"\"
    for i in range(n):
        await ctx.report_progress(i, n, message=f"step {i}")
    await ctx.info("almost done")
    return {"done": n}


@mcp.resource("config://app", name="app-config", mime_type="application/json",
              description="Static app configuration")
async def app_config() -> str:
    return '{"env": "itest"}'


@mcp.resource("users://{user_id}/profile", name="user-profile", mime_type="application/json")
async def user_profile(user_id: int) -> str:
    # user_id is extracted from the URI as a string and must arrive coerced to int.
    return f'{{"id": {user_id}, "type": "{type(user_id).__name__}"}}'


@mcp.prompt
async def summarize(topic: str) -> str:
    \"\"\"Summarize a topic.\"\"\"
    return f"Please summarize: {topic}"


# An ordinary REST route, explicitly exposed as an MCP tool (never implicit).
@api.get("/double/{n}")
async def double(n: int) -> dict:
    \"\"\"Double a number.\"\"\"
    return {"result": n * 2}


mount_mcp(api, mcp, expose=[double])
"""


def _post(server, method, params=None, *, session_id=None, request_id=1):
    return server.client.post(
        server.url("/mcp"), content=rpc_body(method, params, id=request_id), headers=mcp_headers(session_id=session_id)
    )


def _init(server, *, capabilities=None):
    resp = _post(
        server,
        "initialize",
        {"protocolVersion": PROTOCOL, "capabilities": capabilities or {}, "clientInfo": {"name": "it", "version": "1"}},
    )
    assert resp.status_code == 200, resp.text
    return resp.headers.get("mcp-session-id"), parse_rpc(resp)["result"]


# ── handshake / lifecycle ─────────────────────────────────────────────────────--
def test_initialize_handshake_and_capabilities(feature_server):
    session_id, result = _init(feature_server)
    assert session_id  # stateful server issues a session id
    assert result["serverInfo"] == {"name": "features-itest", "version": "2.0"}
    assert result["protocolVersion"] == PROTOCOL
    # Capabilities reflect what was registered: tools, resources, and prompts.
    assert set(result["capabilities"]) == {"tools", "resources", "prompts"}


def test_ping(feature_server):
    sid, _ = _init(feature_server)
    assert parse_rpc(_post(feature_server, "ping", session_id=sid))["result"] == {}


# ── tools ─────────────────────────────────────────────────────────────────────--
def test_tools_list_lists_native_and_exposed_tools(feature_server):
    sid, _ = _init(feature_server)
    listed = _post(feature_server, "tools/list", session_id=sid)
    names = {t["name"] for t in parse_rpc(listed)["result"]["tools"]}
    # native tools plus the explicitly exposed REST route
    assert {"add", "boom", "crunch", "double"} <= names


def test_tool_call_returns_structured_content(feature_server):
    sid, _ = _init(feature_server)
    called = _post(feature_server, "tools/call", {"name": "add", "arguments": {"a": 4, "b": 5}}, session_id=sid)
    result = parse_rpc(called)["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"sum": 9}


def test_exposed_rest_route_is_callable_as_a_tool(feature_server):
    sid, _ = _init(feature_server)
    called = _post(feature_server, "tools/call", {"name": "double", "arguments": {"n": 21}}, session_id=sid)
    assert parse_rpc(called)["result"]["structuredContent"] == {"result": 42}


def test_failing_tool_returns_in_band_error(feature_server):
    """A raised exception is an in-band CallToolResult error, not a JSON-RPC error."""
    sid, _ = _init(feature_server)
    called = _post(feature_server, "tools/call", {"name": "boom", "arguments": {}}, session_id=sid)
    msg = parse_rpc(called)
    assert "error" not in msg  # JSON-RPC envelope is a success
    assert msg["result"]["isError"] is True
    assert "kaboom" in msg["result"]["content"][0]["text"]


def test_context_tool_streams_progress_then_result(feature_server):
    """A Context tool emits progress + log notifications over SSE, then one final result."""
    sid, _ = _init(feature_server)
    resp = feature_server.client.post(
        feature_server.url("/mcp"),
        content=rpc_body(
            "tools/call", {"name": "crunch", "arguments": {"n": 3}, "_meta": {"progressToken": "tok1"}}, id=9
        ),
        headers=mcp_headers(session_id=sid),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_all_sse(resp.content)
    progress = [e for e in events if e.get("method") == "notifications/progress"]
    logs = [e for e in events if e.get("method") == "notifications/message"]
    results = [e for e in events if "result" in e]

    assert [p["params"]["progress"] for p in progress] == [0, 1, 2]
    assert all(p["params"]["progressToken"] == "tok1" for p in progress)
    assert progress[0]["params"]["total"] == 3
    assert len(logs) == 1 and logs[0]["params"]["data"] == "almost done"
    assert len(results) == 1
    assert results[0]["id"] == 9
    assert results[0]["result"]["structuredContent"] == {"done": 3}


# ── resources ───────────────────────────────────────────────────────────────---
def test_resource_list_and_read(feature_server):
    sid, _ = _init(feature_server)
    listed = _post(feature_server, "resources/list", session_id=sid)
    resources = {r["uri"]: r for r in parse_rpc(listed)["result"]["resources"]}
    assert resources["config://app"]["mimeType"] == "application/json"

    read = _post(feature_server, "resources/read", {"uri": "config://app"}, session_id=sid)
    contents = parse_rpc(read)["result"]["contents"]
    assert contents[0]["uri"] == "config://app"
    assert contents[0]["text"] == '{"env": "itest"}'


def test_resource_template_list_and_read_coerces_uri_params(feature_server):
    sid, _ = _init(feature_server)
    templates = parse_rpc(_post(feature_server, "resources/templates/list", session_id=sid))["result"][
        "resourceTemplates"
    ]
    assert any(t["uriTemplate"] == "users://{user_id}/profile" for t in templates)

    read = _post(feature_server, "resources/read", {"uri": "users://42/profile"}, session_id=sid)
    # {user_id} extracted from the URI and coerced from "42" (str) to int.
    assert parse_rpc(read)["result"]["contents"][0]["text"] == '{"id": 42, "type": "int"}'


def test_unknown_resource_is_jsonrpc_error(feature_server):
    sid, _ = _init(feature_server)
    read = _post(feature_server, "resources/read", {"uri": "config://nope"}, session_id=sid)
    assert "error" in parse_rpc(read)


# ── prompts ─────────────────────────────────────────────────────────────────---
def test_prompt_list_and_get(feature_server):
    sid, _ = _init(feature_server)
    listed = _post(feature_server, "prompts/list", session_id=sid)
    prompts = {p["name"]: p for p in parse_rpc(listed)["result"]["prompts"]}
    assert "topic" in {a["name"] for a in prompts["summarize"]["arguments"]}

    got = _post(feature_server, "prompts/get", {"name": "summarize", "arguments": {"topic": "otters"}}, session_id=sid)
    messages = parse_rpc(got)["result"]["messages"]
    assert messages[0]["role"] == "user"
    assert "otters" in messages[0]["content"]["text"]


# ── error framing ─────────────────────────────────────────────────────────────-
def test_unknown_method_returns_jsonrpc_error(feature_server):
    sid, _ = _init(feature_server)
    resp = _post(feature_server, "does/not/exist", session_id=sid)
    err = parse_rpc(resp)["error"]
    assert err["code"] == -32601  # METHOD_NOT_FOUND


# ── transport modes (own server each) ────────────────────────────────────────---
JSON_MODE_API_BODY = """
from bolt_mcp import MCP, mount_mcp

mcp = MCP("json-itest", "1.0", json_response=True)  # single application/json object, no SSE


@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}


mount_mcp(api, mcp)
"""

STATELESS_API_BODY = """
from bolt_mcp import MCP, mount_mcp

mcp = MCP("stateless-itest", "1.0", stateless=True)  # no session, no GET channel


@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}


mount_mcp(api, mcp)
"""


def test_json_response_mode_returns_single_json_object(make_server_project):
    project = make_server_project(project_api_body=JSON_MODE_API_BODY)
    with project.start() as server:
        sid, _ = _init(server)
        called = _post(server, "tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}, session_id=sid)
        assert called.headers["content-type"].startswith("application/json")  # not text/event-stream
        assert parse_rpc(called)["result"]["structuredContent"] == {"sum": 5}


def test_stateless_mode_issues_no_session_and_accepts_calls(make_server_project):
    project = make_server_project(project_api_body=STATELESS_API_BODY)
    with project.start() as server:
        session_id, _ = _init(server)
        assert session_id is None  # stateless: no Mcp-Session-Id header issued
        # A call with no session id is accepted (every request is self-contained).
        called = _post(server, "tools/call", {"name": "add", "arguments": {"a": 6, "b": 1}})
        assert parse_rpc(called)["result"]["structuredContent"] == {"sum": 7}
