"""Tool invocation and MCP result mapping.

Calls sync/async tool functions and maps their return value into an MCP
``CallToolResult`` (``content`` text + ``structuredContent``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import msgspec

from django_bolt._json import default_serializer
from django_bolt._json import encode as json_encode

from .context import McpClientError
from .registry import ToolDef

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """Raise in a tool to send this message to the client as the error result.

    The message always reaches the client, also when ``MCP`` masks other exceptions,
    and is not logged as a failure.
    """


def _text_content(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


# MCP 2026-07-28 requires ``resultType`` on every result. Rust strips it for
# legacy peers.
RESULT_TYPE_COMPLETE = "complete"


def error_result(message: str) -> dict[str, Any]:
    """An in-band CallToolResult error (the MCP way to surface tool failures)."""
    return {"content": [_text_content(message)], "isError": True, "resultType": RESULT_TYPE_COMPLETE}


@dataclass(frozen=True)
class ToolResult:
    """A tool result whose text content is set apart from its structured content.

    ``content`` is the text the model reads, or a list of MCP content blocks.
    ``structured`` becomes ``structuredContent`` (a non-object is wrapped as
    ``{"result": ...}``). ``meta`` becomes the result's ``_meta``.
    """

    content: str | list[dict[str, Any]]
    structured: Any = None
    meta: dict[str, Any] | None = None


def _structured(value: Any) -> dict[str, Any]:
    payload = msgspec.to_builtins(value, enc_hook=default_serializer)
    return payload if isinstance(payload, dict) else {"result": payload}


def _from_tool_result(result: ToolResult) -> dict[str, Any]:
    content = [_text_content(result.content)] if isinstance(result.content, str) else list(result.content)
    mapped: dict[str, Any] = {"content": content, "isError": False, "resultType": RESULT_TYPE_COMPLETE}
    if result.structured is not None:
        mapped["structuredContent"] = _structured(result.structured)
    if result.meta is not None:
        mapped["_meta"] = result.meta
    return mapped


def to_call_tool_result(result: Any) -> dict[str, Any]:
    """Map a tool's return value into a CallToolResult dict."""
    if isinstance(result, ToolResult):
        return _from_tool_result(result)
    if isinstance(result, str):
        return {"content": [_text_content(result)], "isError": False, "resultType": RESULT_TYPE_COMPLETE}
    if isinstance(result, dict):
        text = json_encode(result).decode()
        return {
            "content": [_text_content(text)],
            "structuredContent": result,
            "isError": False,
            "resultType": RESULT_TYPE_COMPLETE,
        }
    payload = msgspec.to_builtins(result, enc_hook=default_serializer)
    structured = payload if isinstance(payload, dict) else {"result": payload}
    text = json_encode(payload).decode()
    return {
        "content": [_text_content(text)],
        "structuredContent": structured,
        "isError": False,
        "resultType": RESULT_TYPE_COMPLETE,
    }


async def run_tool(tool: ToolDef, kwargs: dict[str, Any]) -> Any:
    """Invoke a tool and return its result value (awaiting async, off-loading sync)."""
    if tool.is_async:
        return await tool.fn(**kwargs)
    return await asyncio.to_thread(tool.fn, **kwargs)


async def execute_tool(tool: ToolDef, kwargs: dict[str, Any], *, mask_error_details: bool) -> dict[str, Any]:
    """Run a tool and map its return value (or exception) to a CallToolResult dict.

    A raised exception becomes an in-band error result, per MCP tool semantics —
    shared by the non-streaming and streaming call paths. ``ToolError`` and
    ``McpClientError`` send their message. Any other exception is logged with its
    traceback, and its text is replaced when ``mask_error_details`` is set.
    """
    try:
        return to_call_tool_result(await run_tool(tool, kwargs))
    except (ToolError, McpClientError) as exc:
        return error_result(str(exc))
    except Exception as exc:
        logger.exception("MCP tool %r failed", tool.name)
        return error_result(f"Tool {tool.name!r} failed." if mask_error_details else str(exc))
