"""Explicit handler allowlist: ``api.mount_mcp(mcp, expose=[handler, ...])``."""

from __future__ import annotations

import pytest
from _helpers import initialize, parse_rpc, post_rpc
from bolt_mcp import MCP, expose_as_tool

from django_bolt import BoltAPI
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
