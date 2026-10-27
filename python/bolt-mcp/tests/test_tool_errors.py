"""Errors from tools, resources and prompts: ``ToolError``, logging and ``mask_error_details``."""

from __future__ import annotations

import logging

import pytest
from _helpers import initialize, parse_rpc, post_rpc
from bolt_mcp import MCP, ToolError, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

SECRET = "connection to db.internal:5432 refused"


def _server(*, mask: bool):
    api = BoltAPI()
    mcp = MCP("errors-test", mask_error_details=mask)

    @mcp.tool
    def no_invoice(invoice_id: int) -> dict:
        raise ToolError(f"No invoice {invoice_id}.")

    @mcp.tool
    def broken() -> dict:
        raise RuntimeError(SECRET)

    @mcp.resource("config://broken")
    def broken_resource() -> str:
        raise RuntimeError(SECRET)

    @mcp.prompt
    def broken_prompt() -> str:
        raise RuntimeError(SECRET)

    mount_mcp(api, mcp, path="/mcp")
    return api


def _post(client, method: str, params: dict) -> dict:
    _, session_id = initialize(client)
    return parse_rpc(post_rpc(client, method, params, session_id=session_id))


def _tool(client, name: str, arguments: dict) -> dict:
    return _post(client, "tools/call", {"name": name, "arguments": arguments})["result"]


@pytest.mark.parametrize("mask", [False, True])
def test_tool_error_message_reaches_the_client_and_is_not_logged(mask, caplog):
    with caplog.at_level(logging.ERROR), TestClient(_server(mask=mask)) as client:
        result = _tool(client, "no_invoice", {"invoice_id": 7})
    assert result["isError"] is True
    assert result["content"][0]["text"] == "No invoice 7."
    assert not [r for r in caplog.records if r.name == "bolt_mcp._execute"]


def test_other_exception_is_logged_with_its_traceback(caplog):
    with caplog.at_level(logging.ERROR), TestClient(_server(mask=False)) as client:
        _tool(client, "broken", {})
    (record,) = [r for r in caplog.records if r.name == "bolt_mcp._execute"]
    assert record.getMessage() == "MCP tool 'broken' failed"
    assert record.exc_info is not None
    assert SECRET in str(record.exc_info[1])


def test_other_exception_text_reaches_the_client_by_default():
    with TestClient(_server(mask=False)) as client:
        result = _tool(client, "broken", {})
    assert result["isError"] is True
    assert result["content"][0]["text"] == SECRET


def test_mask_error_details_replaces_the_exception_text():
    with TestClient(_server(mask=True)) as client:
        result = _tool(client, "broken", {})
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Tool 'broken' failed."


@pytest.mark.parametrize(
    ("method", "params", "masked"),
    [
        ("resources/read", {"uri": "config://broken"}, "Resource 'config://broken' failed."),
        ("prompts/get", {"name": "broken_prompt"}, "Prompt 'broken_prompt' failed."),
    ],
)
def test_resource_and_prompt_exceptions_are_logged_and_masked(method, params, masked, caplog):
    with caplog.at_level(logging.ERROR), TestClient(_server(mask=True)) as client:
        body = _post(client, method, params)
    assert body["error"]["code"] == -32603
    assert body["error"]["message"] == masked
    (record,) = [r for r in caplog.records if r.name == "bolt_mcp._dispatch"]
    assert record.exc_info is not None


@pytest.mark.parametrize(
    ("method", "params"),
    [("resources/read", {"uri": "config://broken"}), ("prompts/get", {"name": "broken_prompt"})],
)
def test_resource_and_prompt_exception_text_is_kept_by_default(method, params):
    with TestClient(_server(mask=False)) as client:
        body = _post(client, method, params)
    assert body["error"]["code"] == -32603
    assert body["error"]["message"] == f"MCP dispatch failed: RuntimeError: {SECRET}"
