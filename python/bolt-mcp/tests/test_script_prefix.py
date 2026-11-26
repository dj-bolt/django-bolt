"""FORCE_SCRIPT_NAME: tools, resources and prompts run with the script prefix, as a route does."""

from __future__ import annotations

import asyncio

from _helpers import parse_rpc, post_rpc_modern
from bolt_mcp import MCP
from django.test import override_settings
from django.urls import get_script_prefix

from django_bolt import BoltAPI, Request
from django_bolt.testing import TestClient


def _api() -> BoltAPI:
    api = BoltAPI()
    mcp = MCP(name="prefix")

    @mcp.tool
    async def prefix() -> dict:
        """Give the script prefix."""
        before = get_script_prefix()
        await asyncio.sleep(0)
        return {"before": before, "after_await": get_script_prefix()}

    @mcp.resource("prefix://now", mime_type="text/plain")
    def resource() -> str:
        """Give the script prefix."""
        return get_script_prefix()

    @mcp.prompt
    def prompt() -> str:
        """Give the script prefix."""
        return get_script_prefix()

    @api.get("/path")
    async def request_path(request: Request) -> dict:
        """Give the path of the request."""
        return {"path": request.path, "path_info": request.path_info}

    api.mount_mcp(mcp, expose=[request_path])
    return api


def _results(api: BoltAPI) -> list:
    with TestClient(api) as client:
        tool = parse_rpc(post_rpc_modern(client, "tools/call", {"name": "prefix", "arguments": {}}))
        exposed = parse_rpc(post_rpc_modern(client, "tools/call", {"name": "request_path", "arguments": {}}))
        read = parse_rpc(post_rpc_modern(client, "resources/read", {"uri": "prefix://now"}))
        got = parse_rpc(post_rpc_modern(client, "prompts/get", {"name": "prompt", "arguments": {}}))
    return [
        tool["result"]["structuredContent"],
        exposed["result"]["structuredContent"],
        read["result"]["contents"][0]["text"],
        got["result"]["messages"][0]["content"]["text"],
    ]


def test_mcp_calls_run_with_the_script_prefix_of_the_setting():
    with override_settings(FORCE_SCRIPT_NAME="/myapp"):
        with_setting = _results(_api())
    without_setting = _results(_api())

    assert with_setting == [
        {"before": "/myapp/", "after_await": "/myapp/"},
        {"path": "/myapp/mcp", "path_info": "/mcp"},
        "/myapp/",
        "/myapp/",
    ]
    assert without_setting == [{"before": "/", "after_await": "/"}, {"path": "/mcp", "path_info": "/mcp"}, "/", "/"]
