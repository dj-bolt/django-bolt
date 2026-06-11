"""``api.mount_mcp(mcp)`` — the one-call method form and its exposure semantics."""

from __future__ import annotations

import pytest
from _helpers import initialize, parse_rpc, post_rpc
from bolt_mcp import MCP, expose_as_tool

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


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
