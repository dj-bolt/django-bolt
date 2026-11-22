"""Explicit handler allowlist: ``api.mount_mcp(mcp, expose=[handler, ...])``."""

from __future__ import annotations

import pytest
from _helpers import initialize, mint_jwt, parse_rpc, post_rpc
from bolt_mcp import MCP, expose_as_tool, expose_routes, mount_mcp
from django.test import override_settings

from django_bolt import BoltAPI, IsAuthenticated, JWTAuthentication
from django_bolt.testing import TestClient


def _tools_list(api) -> set[str]:
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "tools/list", session_id=session_id)
        return {t["name"] for t in parse_rpc(resp)["result"]["tools"]}


def test_explicit_handler_exposed_without_marker():
    """A handler passed in the allowlist is exposed even with no @expose_as_tool."""
    api = BoltAPI()
    mcp = MCP("explicit-server")

    @api.get("/items/{item_id}")
    async def get_item(item_id: int) -> dict:
        return {"id": item_id}

    @api.get("/secret/{x}")
    async def secret(x: int) -> dict:
        return {"x": x}

    api.mount_mcp(mcp, expose=[get_item])

    tools = _tools_list(api)
    assert len(tools) == 1  # only the listed handler, no marker required
    assert not any("secret" in name for name in tools)  # the unlisted route stays private


def test_explicit_handler_callable_and_binds_param():
    api = BoltAPI()
    mcp = MCP("explicit-call-server")

    @api.get("/double/{n}")
    async def double(n: int) -> dict:
        return {"result": n * 2}

    api.mount_mcp(mcp, expose=[double])

    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "tools/list", session_id=session_id)
        (name,) = [t["name"] for t in parse_rpc(resp)["result"]["tools"]]
        resp = post_rpc(
            client,
            "tools/call",
            {"name": name, "arguments": {"n": 21}},
            session_id=session_id,
        )
        assert parse_rpc(resp)["result"]["structuredContent"] == {"result": 42}


def test_unregistered_handler_raises():
    """Naming a handler that isn't a route on this api is a programming error."""
    api = BoltAPI()
    mcp = MCP("explicit-bad-server")

    async def not_a_route(x: int) -> dict:
        return {"x": x}

    with pytest.raises(ValueError, match="not registered"):
        api.mount_mcp(mcp, expose=[not_a_route])


def test_name_and_description_derived_from_route():
    """No @expose_as_tool: tool name = function name, description = docstring."""
    api = BoltAPI()
    mcp = MCP("derive-server")

    @api.get("/widgets/{wid}")
    async def get_widget(wid: int) -> dict:
        """Fetch a widget by id."""
        return {"wid": wid}

    api.mount_mcp(mcp, expose=[get_widget])

    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "tools/list", session_id=session_id)
        (tool,) = parse_rpc(resp)["result"]["tools"]
        assert tool["name"] == "get_widget"
        assert tool["description"] == "Fetch a widget by id."


def test_duplicate_tool_name_raises():
    """Two routes resolving to the same tool name must error, not silently shadow."""
    api = BoltAPI()
    mcp = MCP("dup-server")

    @api.get("/a/{x}")
    @expose_as_tool(name="thing")
    async def a(x: int) -> dict:
        return {"x": x}

    @api.get("/b/{y}")
    @expose_as_tool(name="thing")
    async def b(y: int) -> dict:
        return {"y": y}

    with pytest.raises(ValueError, match="already registered"):
        api.mount_mcp(mcp, expose=[a, b])


SECRET = "expose-secret-key-0123456789-abcdefgh"


def _guarded_api(expose_with_marker: bool):
    """A route with auth and guards, exposed on a mount whose auth accepts the same token."""
    api = BoltAPI()
    mcp = MCP("guarded-expose-server")

    @api.delete("/articles/{article_id}", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    @expose_as_tool()
    async def delete_article(article_id: int) -> dict:
        return {"deleted": article_id}

    if expose_with_marker:
        expose_routes(mcp, api, methods=("DELETE",))
        mount_mcp(api, mcp, auth=[JWTAuthentication(secret=SECRET)])
    else:
        api.mount_mcp(mcp, auth=[JWTAuthentication(secret=SECRET)], expose=[delete_article])
    return api


def _call(client, authorization: str | None = None) -> dict:
    _, session_id = initialize(client, authorization=authorization)
    resp = post_rpc(
        client,
        "tools/call",
        {"name": "delete_article", "arguments": {"article_id": 3}},
        session_id=session_id,
        authorization=authorization,
    )
    return parse_rpc(resp)["result"]


@pytest.mark.parametrize("expose_with_marker", [False, True], ids=["expose", "expose_routes"])
def test_an_exposed_route_keeps_its_guards(expose_with_marker):
    """An anonymous MCP client cannot call a route that answers 401 over HTTP."""
    api = _guarded_api(expose_with_marker)
    with TestClient(api) as client:
        assert client.delete("/articles/3").status_code == 401
        result = _call(client)
        assert result["isError"] is True
        assert "structuredContent" not in result


@pytest.mark.parametrize("expose_with_marker", [False, True], ids=["expose", "expose_routes"])
def test_an_exposed_route_runs_for_a_principal_that_passes_its_guards(expose_with_marker):
    api = _guarded_api(expose_with_marker)
    bearer = f"Bearer {mint_jwt(SECRET, sub='7')}"
    with TestClient(api) as client:
        assert client.delete("/articles/3", headers={"Authorization": bearer}).status_code == 200
        assert _call(client, bearer)["structuredContent"] == {"deleted": 3}


def test_an_exposed_route_keeps_the_default_permission_classes():
    """A route with no ``guards`` gets BOLT_DEFAULT_PERMISSION_CLASSES over MCP too."""
    with override_settings(BOLT_DEFAULT_PERMISSION_CLASSES=[IsAuthenticated()]):
        api = BoltAPI()
        mcp = MCP("default-guard-expose-server")

        @api.delete("/articles/{article_id}", auth=[JWTAuthentication(secret=SECRET)])
        async def delete_article(article_id: int) -> dict:
            return {"deleted": article_id}

        api.mount_mcp(mcp, auth=[JWTAuthentication(secret=SECRET)], expose=[delete_article])
    with TestClient(api) as client:
        assert client.delete("/articles/3").status_code == 401
        assert _call(client)["isError"] is True
