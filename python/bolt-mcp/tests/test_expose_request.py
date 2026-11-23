"""An exposed route that takes ``request`` gets a request for each tool call (#948)."""

from __future__ import annotations

import uuid

import pytest
from _helpers import mint_jwt, parse_rpc, post_rpc_modern
from bolt_mcp import MCP, Context, expose_as_tool, expose_routes
from django.contrib.auth import get_user_model

from django_bolt import BoltAPI, Depends, IsAuthenticated, JWTAuthentication, Request
from django_bolt.testing import TestClient

SECRET = "expose-request-secret-key-0123456789-abcdef"


def _call(client, name: str, arguments: dict | None = None, **kwargs) -> dict:
    resp = post_rpc_modern(client, "tools/call", {"name": name, "arguments": arguments or {}}, **kwargs)
    assert resp.status_code == 200
    result = parse_rpc(resp)["result"]
    assert result["isError"] is False, result
    return result["structuredContent"]


@pytest.fixture
def user():
    user = get_user_model().objects.create_user(username=f"mcp-{uuid.uuid4().hex[:12]}", password="x")
    yield user
    user.delete()


def test_an_async_handler_with_a_typed_request_gets_the_request():
    """The example of #948."""
    api = BoltAPI()

    @api.get("/me")
    async def me(request: Request) -> dict:
        """Read the path."""
        return {"path": request.path, "method": request.method, "trace": request.headers.get("x-trace")}

    api.mount_mcp(MCP(name="t"), expose=[me])
    with TestClient(api) as client:
        assert _call(client, "me", headers_override={"X-Trace": "abc"}) == {
            "path": "/mcp",
            "method": "POST",
            "trace": "abc",
        }


def test_a_sync_handler_with_an_untyped_request_gets_the_request():
    api = BoltAPI()

    @api.get("/agent")
    def agent(request) -> dict:
        return {"agent": request.headers.get("user-agent"), "body": request.body, "query": dict(request.query)}

    api.mount_mcp(MCP(name="t"), expose=[agent])
    with TestClient(api) as client:
        result = _call(client, "agent", headers_override={"User-Agent": "mcp-client/1.0"})
    assert result == {"agent": "mcp-client/1.0", "body": "", "query": {}}


def test_a_request_named_req_with_other_parameters_gets_the_request():
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def get_item(item_id: int, req, verbose: bool = False) -> dict:
        return {"id": item_id, "verbose": verbose, "trace": req.headers.get("x-trace"), "params": req.params}

    api.mount_mcp(MCP(name="t"), expose=[get_item])
    with TestClient(api) as client:
        result = _call(client, "get_item", {"item_id": 5, "verbose": True}, headers_override={"X-Trace": "t-1"})
    assert result == {"id": 5, "verbose": True, "trace": "t-1", "params": {}}


def test_a_jwt_route_gives_the_user_of_its_token(user):
    api = BoltAPI()

    @api.get("/whoami", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    def whoami(request: Request) -> dict:
        return {
            "username": request.user.username,
            "user_id": request.context["user_id"],
            "backend": request.context["auth_backend"],
        }

    @api.get("/awhoami", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    async def awhoami(request: Request) -> dict:
        user = await request.auser()
        return {"username": user.username, "authenticated": user.is_authenticated}

    api.mount_mcp(MCP(name="t"), expose=[whoami, awhoami])
    bearer = f"Bearer {mint_jwt(SECRET, sub=str(user.pk))}"
    with TestClient(api) as client:
        over_http = client.get("/whoami", headers={"Authorization": bearer}).json()
        assert _call(client, "whoami", authorization=bearer) == over_http
        assert over_http == {"username": user.username, "user_id": str(user.pk), "backend": "jwt"}
        assert _call(client, "awhoami", authorization=bearer) == {"username": user.username, "authenticated": True}


def test_a_public_route_gives_the_anonymous_user():
    api = BoltAPI()

    @api.get("/hello")
    async def hello(request: Request) -> dict:
        user = await request.auser()
        return {
            "anonymous": request.user.is_anonymous,
            "same": user is request.user,
            "context": request.context,
        }

    api.mount_mcp(MCP(name="t"), expose=[hello])
    with TestClient(api) as client:
        over_http = client.get("/hello").json()
        assert _call(client, "hello") == over_http == {"anonymous": True, "same": True, "context": None}


def test_a_public_route_on_a_mount_with_auth_gives_the_anonymous_user(user):
    """The principal of the route replaces that of the mount (#939), as over HTTP."""
    api = BoltAPI()

    @api.get("/hello")
    async def hello(request: Request) -> dict:
        return {"anonymous": request.user.is_anonymous, "context": request.context}

    api.mount_mcp(MCP(name="t"), expose=[hello], auth=[JWTAuthentication(secret=SECRET)])
    bearer = f"Bearer {mint_jwt(SECRET, sub=str(user.pk))}"
    with TestClient(api) as client:
        over_http = client.get("/hello", headers={"Authorization": bearer}).json()
        assert _call(client, "hello", authorization=bearer) == over_http == {"anonymous": True, "context": None}


def test_a_marked_handler_gets_the_request():
    api = BoltAPI()
    mcp = MCP(name="t")

    @api.get("/marked")
    @expose_as_tool(name="marked_tool")
    async def marked(request: Request) -> dict:
        return {"trace": request.headers.get("x-trace")}

    expose_routes(mcp, api)
    api.mount_mcp(mcp)
    with TestClient(api) as client:
        assert _call(client, "marked_tool", headers_override={"X-Trace": "m"}) == {"trace": "m"}


def test_a_native_tool_gets_the_headers_and_the_user_of_the_request(user):
    api = BoltAPI()
    mcp = MCP(name="t")

    @mcp.tool
    async def whoami(request) -> dict:
        user = await request.auser()
        return {"username": user.username, "trace": request.headers.get("x-trace"), "state": request.state}

    api.mount_mcp(mcp, auth=[JWTAuthentication(secret=SECRET)])
    bearer = f"Bearer {mint_jwt(SECRET, sub=str(user.pk))}"
    with TestClient(api) as client:
        result = _call(client, "whoami", authorization=bearer, headers_override={"X-Trace": "n"})
    assert result == {"username": user.username, "trace": "n", "state": {}}


def test_a_handler_with_a_dependency_is_refused_at_mount():
    api = BoltAPI()

    async def current_tenant(request: Request) -> str:
        return request.headers.get("x-tenant", "")

    @api.get("/tenant")
    async def tenant(name: str = Depends(current_tenant)) -> dict:
        return {"tenant": name}

    with pytest.raises(ValueError, match=r"'tenant'.*dependency"):
        api.mount_mcp(MCP(name="t"), expose=[tenant])


def test_the_request_has_the_client_information_of_the_mcp_request():
    api = BoltAPI()

    @api.get("/client")
    def client_info(request: Request) -> dict:
        return {
            "remote": request.META["REMOTE_ADDR"],
            "scheme": request.scheme,
            "host": request.META["HTTP_HOST"],
            "session": request.cookies.get("session"),
        }

    api.mount_mcp(MCP(name="t"), expose=[client_info])
    with TestClient(api) as client:
        over_http = client.get("/client", headers={"Cookie": "session=abc"}).json()
        assert over_http["remote"]
        assert _call(client, "client_info", headers_override={"Cookie": "session=abc"}) == over_http


def test_each_call_gets_a_new_request():
    api = BoltAPI()
    mcp = MCP(name="t")

    @mcp.tool
    def count(req) -> dict:
        req.state["calls"] = req.state.get("calls", 0) + 1
        return {"calls": req.state["calls"]}

    api.mount_mcp(mcp)
    with TestClient(api) as client:
        assert _call(client, "count") == {"calls": 1}
        assert _call(client, "count") == {"calls": 1}


def test_the_context_of_a_tool_holds_the_request():
    api = BoltAPI()
    mcp = MCP(name="t")

    @mcp.tool
    async def traced(ctx: Context) -> dict:
        return {"trace": ctx.request.headers.get("x-trace"), "anonymous": ctx.request.user.is_anonymous}

    api.mount_mcp(mcp)
    with TestClient(api) as client:
        assert _call(client, "traced", headers_override={"X-Trace": "c"}) == {"trace": "c", "anonymous": True}
