"""``api.mount_mcp(mcp)`` — the one-call method form and its exposure semantics."""

from __future__ import annotations

import pytest
from _helpers import INITIALIZE_PARAMS, initialize, mcp_headers, parse_rpc, post_rpc, rpc_body
from bolt_mcp import MCP, AuthorizationServer, expose_as_tool

from django_bolt import BoltAPI, _core
from django_bolt.testing import AsyncTestClient, TestClient


def _tools(api) -> set[str]:
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "tools/list", session_id=session_id)
        return {t["name"] for t in parse_rpc(resp)["result"]["tools"]}


def _build(expose):
    api = BoltAPI()
    mcp = MCP("mount-method-server")

    @mcp.tool
    async def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    @api.get("/items/{item_id}")
    @expose_as_tool(name="get_item", description="Fetch an item by id")
    async def get_item(item_id: int) -> dict:
        return {"id": item_id}

    if expose is _DEFAULT:
        api.mount_mcp(mcp)
    else:
        api.mount_mcp(mcp, expose=expose)
    return api


_DEFAULT = object()


def test_default_serves_native_tools_only():
    """Bare ``api.mount_mcp(mcp)`` serves native @mcp.tool but does NOT implicitly
    expose REST routes — even ones marked with @expose_as_tool."""
    tools = _tools(_build(_DEFAULT))
    assert "add" in tools
    assert "get_item" not in tools


def test_expose_true_is_rejected():
    """There is no expose-everything switch — ``expose=True`` must raise, not bulk-expose."""
    with pytest.raises(TypeError, match="explicit list"):
        _build(True)


def test_expose_false_serves_native_only():
    tools = _tools(_build(False))
    assert "add" in tools
    assert "get_item" not in tools


def test_unsupported_oauth_value_is_rejected_at_mount_time():
    """``oauth=`` accepts only AuthorizationServer/ProtectedResource — anything else
    must fail at mount time, not on the first request."""
    api = BoltAPI()
    mcp = MCP("mount-method-server")
    with pytest.raises(TypeError, match="ProtectedResource"):
        api.mount_mcp(mcp, oauth=object())


def test_duplicate_mount_rejected_before_oauth_routes_are_registered():
    api = BoltAPI()
    api.mount_mcp(MCP("first"))
    route_count = len(api._routes)

    with pytest.raises(ValueError, match="already mounted"):
        api.mount_mcp(MCP("second"), oauth=AuthorizationServer())

    assert len(api._routes) == route_count


def test_authorization_server_cannot_be_reused_at_a_different_mount_path():
    server = AuthorizationServer(issuer="https://api.example.test")
    first = BoltAPI()
    first.mount_mcp(MCP("first"), path="/one", oauth=server)

    second = BoltAPI()
    with pytest.raises(ValueError, match="already bound"):
        second.mount_mcp(MCP("second"), path="/two", oauth=server)
    assert second._routes == []


def test_authorization_server_reuse_requires_the_same_resource_url():
    server = AuthorizationServer(issuer="https://api.example.test")
    first = BoltAPI()
    first.mount_mcp(MCP("first"), oauth=server)
    server.resource_url = "https://other.example.test/mcp"

    second = BoltAPI()
    with pytest.raises(ValueError, match="already bound to the MCP resource"):
        second.mount_mcp(MCP("second"), oauth=server)
    assert second._routes == []


def test_child_mcp_mount_is_available_under_parent_prefix():
    child = BoltAPI()
    child.mount_mcp(MCP("child"))
    parent = BoltAPI()
    parent.mount("/child", child)

    with TestClient(parent) as client:
        response = client.post(
            "/child/mcp",
            content=rpc_body("initialize", INITIALIZE_PARAMS),
            headers=mcp_headers(),
        )
    assert response.status_code == 200
    assert parent._mcp_mounts[0]["path"] == "/child/mcp"


def test_child_oauth_mcp_mount_is_rejected_before_parent_mutation():
    child = BoltAPI()
    child.mount_mcp(MCP("child"), oauth=AuthorizationServer(issuer="https://api.example.test"))
    parent = BoltAPI()

    with pytest.raises(ValueError, match="OAuth-protected MCP server"):
        parent.mount("/child", child)

    assert parent._routes == []
    assert parent._mcp_mounts == []


def test_testclient_rejects_mcp_http_route_collision():
    api = BoltAPI()
    api.mount_mcp(MCP("server"))

    @api.post("/mcp")
    async def route():
        return {"ok": True}

    with pytest.raises(ValueError, match="HTTP route"):
        TestClient(api)


def test_testclient_rejects_mcp_asgi_mount_collision():
    api = BoltAPI()
    api.mount_mcp(MCP("server"))

    async def asgi_app(scope, receive, send):
        pass

    api.mount_asgi("/mcp", asgi_app)
    with pytest.raises(ValueError, match="ASGI mount"):
        TestClient(api)


@pytest.mark.parametrize("client_type", [TestClient, AsyncTestClient])
def test_testclient_rejects_mount_collision_before_native_app_allocation(monkeypatch, client_type):
    api = BoltAPI()
    api.mount_mcp(MCP("server"))

    @api.post("/mcp")
    async def route():
        return {"ok": True}

    def fail_create_test_app(*args, **kwargs):
        pytest.fail("native test app was allocated before mount validation")

    monkeypatch.setattr(_core, "create_test_app", fail_create_test_app)
    with pytest.raises(ValueError, match="HTTP route"):
        client_type(api)
