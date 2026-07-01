"""Tool invocation and MCP result mapping.

Calls sync/async tool functions and maps their return value into an MCP
``CallToolResult`` (``content`` text + ``structuredContent``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import msgspec

from django_bolt._json import default_serializer
from django_bolt._json import encode as json_encode

from .registry import ToolDef


def _text_content(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def error_result(message: str) -> dict[str, Any]:
    """An in-band CallToolResult error (the MCP way to surface tool failures)."""
    return {"content": [_text_content(message)], "isError": True}


def to_call_tool_result(result: Any) -> dict[str, Any]:
    """Map a tool's return value into a CallToolResult dict."""
    if isinstance(result, str):
        return {"content": [_text_content(result)], "isError": False}
    if isinstance(result, dict):
        text = json_encode(result).decode()
        return {"content": [_text_content(text)], "structuredContent": result, "isError": False}
    payload = msgspec.to_builtins(result, enc_hook=default_serializer)
    structured = payload if isinstance(payload, dict) else {"result": payload}
    text = json_encode(payload).decode()
    return {"content": [_text_content(text)], "structuredContent": structured, "isError": False}


async def run_tool(tool: ToolDef, kwargs: dict[str, Any]) -> Any:
    """Invoke a tool and return its result value (awaiting async, off-loading sync)."""
    if tool.is_async:
        return await tool.fn(**kwargs)
    return await asyncio.to_thread(tool.fn, **kwargs)


async def execute_tool(tool: ToolDef, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Run a tool and map its return value (or exception) to a CallToolResult dict.

    A raised exception becomes an in-band error result, per MCP tool semantics —
    shared by the non-streaming and streaming call paths.
    """
    try:
        return to_call_tool_result(await run_tool(tool, kwargs))
    except Exception as exc:
        return error_result(str(exc))
