"""tools/call — ``ToolResult`` sets the text apart from ``structuredContent``."""

from __future__ import annotations

import msgspec
import pytest
from _helpers import initialize, parse_rpc, post_rpc, post_rpc_modern
from bolt_mcp import MCP, ToolResult, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


class Day(msgspec.Struct):
    date: str
    high_c: int


def _server():
    api = BoltAPI()
    mcp = MCP("tool-result-test")

    @mcp.tool
    def forecast(city: str) -> ToolResult:
        days = [Day("2026-10-27", 12), Day("2026-10-28", 14)]
        return ToolResult(
            content=f"{len(days)} days of forecast for {city}.",
            structured={"days": days},
            meta={"ui/source": "test"},
        )

    @mcp.tool
    def blocks() -> ToolResult:
        return ToolResult(content=[{"type": "image", "data": "aGk=", "mimeType": "image/png"}])

    @mcp.tool
    def listed() -> ToolResult:
        return ToolResult(content="three numbers", structured=[1, 2, 3])

    mount_mcp(api, mcp, path="/mcp")
    return api


def _call(client, name: str, arguments: dict) -> dict:
    _, session_id = initialize(client)
    resp = post_rpc(client, "tools/call", {"name": name, "arguments": arguments}, session_id=session_id)
    return parse_rpc(resp)["result"]


def test_text_is_set_apart_from_structured_content():
    with TestClient(_server()) as client:
        result = _call(client, "forecast", {"city": "Leeds"})
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "2 days of forecast for Leeds."}]
        assert result["structuredContent"] == {
            "days": [{"date": "2026-10-27", "high_c": 12}, {"date": "2026-10-28", "high_c": 14}]
        }
        assert result["_meta"] == {"ui/source": "test"}


def test_modern_client_gets_the_same_result():
    with TestClient(_server()) as client:
        resp = post_rpc_modern(client, "tools/call", {"name": "forecast", "arguments": {"city": "Leeds"}})
        result = parse_rpc(resp)["result"]
        assert result["content"] == [{"type": "text", "text": "2 days of forecast for Leeds."}]
        assert result["structuredContent"]["days"][0] == {"date": "2026-10-27", "high_c": 12}
        assert result["_meta"] == {"ui/source": "test"}


def test_content_blocks_are_sent_as_given_without_structured_content():
    with TestClient(_server()) as client:
        result = _call(client, "blocks", {})
        assert result["content"] == [{"type": "image", "data": "aGk=", "mimeType": "image/png"}]
        assert "structuredContent" not in result
        assert "_meta" not in result


def test_structured_value_that_is_not_an_object_is_wrapped():
    with TestClient(_server()) as client:
        result = _call(client, "listed", {})
        assert result["structuredContent"] == {"result": [1, 2, 3]}


def test_auto_output_schema_rejects_tool_result():
    mcp = MCP("tool-result-schema-test")
    with pytest.raises(TypeError, match="returns ToolResult"):

        @mcp.tool(output_schema="auto")
        def summary() -> ToolResult:
            return ToolResult(content="x")
