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

    @mcp.tool
    def sync_prefix() -> dict:
        """Give the script prefix, from a thread."""
        return {"prefix": get_script_prefix()}

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

    @api.get("/sync-path")
    def sync_request_path(request: Request) -> dict:
        """Give the path of the request and the script prefix, from a thread."""
        return {"path": request.path, "prefix": get_script_prefix()}

    api.mount_mcp(mcp, expose=[request_path, sync_request_path])
    return api


def _results(api: BoltAPI) -> list:
    with TestClient(api) as client:
        tool = parse_rpc(post_rpc_modern(client, "tools/call", {"name": "prefix", "arguments": {}}))
        sync_tool = parse_rpc(post_rpc_modern(client, "tools/call", {"name": "sync_prefix", "arguments": {}}))
        exposed = parse_rpc(post_rpc_modern(client, "tools/call", {"name": "request_path", "arguments": {}}))
        sync_exposed = parse_rpc(post_rpc_modern(client, "tools/call", {"name": "sync_request_path", "arguments": {}}))
        read = parse_rpc(post_rpc_modern(client, "resources/read", {"uri": "prefix://now"}))
        got = parse_rpc(post_rpc_modern(client, "prompts/get", {"name": "prompt", "arguments": {}}))
    return [
        tool["result"]["structuredContent"],
        sync_tool["result"]["structuredContent"],
        exposed["result"]["structuredContent"],
        sync_exposed["result"]["structuredContent"],
        read["result"]["contents"][0]["text"],
        got["result"]["messages"][0]["content"]["text"],
    ]


def test_mcp_calls_run_with_the_script_prefix_of_the_setting():
    with override_settings(FORCE_SCRIPT_NAME="/myapp"):
        with_setting = _results(_api())
    without_setting = _results(_api())

    assert with_setting == [
        {"before": "/myapp/", "after_await": "/myapp/"},
        {"prefix": "/myapp/"},
        {"path": "/myapp/mcp", "path_info": "/mcp"},
        {"path": "/myapp/mcp", "prefix": "/myapp/"},
        "/myapp/",
        "/myapp/",
    ]
    assert without_setting == [
        {"before": "/", "after_await": "/"},
        {"prefix": "/"},
        {"path": "/mcp", "path_info": "/mcp"},
        {"path": "/mcp", "prefix": "/"},
        "/",
        "/",
    ]


# The names of the parameters of the functions that give a call the script prefix.
_KEYWORDS = ("fn", "script_prefix", "self", "context", "func", "args", "kwargs")


def _keyword_api() -> BoltAPI:
    api = BoltAPI()
    mcp = MCP(name="keywords")
    for name in _KEYWORDS:
        namespace: dict = {}
        exec(
            f"def tool_{name}({name}: str) -> dict:\n    'Give the argument.'\n    return {{'got': {name}}}\n",
            namespace,
        )
        mcp.tool(namespace[f"tool_{name}"])

    @api.get("/search")
    def search(fn: str, script_prefix: str) -> dict:
        """Give the query parameters."""
        return {"got": fn + script_prefix}

    api.mount_mcp(mcp, expose=[search])
    return api


def _keyword_results() -> dict:
    with TestClient(_keyword_api()) as client:
        calls = {f"tool_{name}": {name: "v"} for name in _KEYWORDS}
        calls["search"] = {"fn": "a", "script_prefix": "b"}
        replies = {
            tool: parse_rpc(post_rpc_modern(client, "tools/call", {"name": tool, "arguments": arguments}))["result"]
            for tool, arguments in calls.items()
        }
    return {tool: (result.get("isError", False), result.get("structuredContent")) for tool, result in replies.items()}


def test_a_sync_tool_gets_each_keyword_argument_with_the_setting():
    with override_settings(FORCE_SCRIPT_NAME="/myapp"):
        with_setting = _keyword_results()
    without_setting = _keyword_results()

    expected = {f"tool_{name}": (False, {"got": "v"}) for name in _KEYWORDS}
    expected["search"] = (False, {"got": "ab"})
    assert with_setting == without_setting == expected
