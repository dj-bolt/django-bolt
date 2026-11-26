"""Tool invocation and MCP result mapping.

Calls sync/async tool functions and maps their return value into an MCP
``CallToolResult`` (``content`` text + ``structuredContent``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import msgspec

from django_bolt._json import decode as json_decode
from django_bolt._json import default_serializer
from django_bolt._json import encode as json_encode
from django_bolt.serialization import (
    _BODY_BYTES,
    _RESPONSE_META_EMPTY,
    _RESPONSE_META_HTML,
    _RESPONSE_META_JSON,
    _RESPONSE_META_OCTETSTREAM,
    _RESPONSE_META_PLAINTEXT,
    ResponseWireV1,
    _is_json_media_type,
)

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
    # Convert once for both outputs. A second pass gets a used-up map, zip or generator.
    # A msgspec.Raw value is JSON already, so it stays as it is.
    payload = msgspec.to_builtins(result, builtin_types=(msgspec.Raw,), enc_hook=default_serializer)
    structured = payload if isinstance(payload, dict) else {"result": payload}
    text = json_encode(payload).decode()
    return {
        "content": [_text_content(text)],
        "structuredContent": structured,
        "isError": False,
        "resultType": RESULT_TYPE_COMPLETE,
    }


# The media type of each static meta tag and each response type of a ResponseWireV1.
_TAG_MEDIA_TYPES = {
    _RESPONSE_META_JSON: "application/json",
    _RESPONSE_META_PLAINTEXT: "text/plain",
    _RESPONSE_META_OCTETSTREAM: "application/octet-stream",
    _RESPONSE_META_EMPTY: "",
    _RESPONSE_META_HTML: "text/html",
}
_TYPE_MEDIA_TYPES = {"json": "application/json", "plaintext": "text/plain", "html": "text/html", "empty": ""}


def _content_type(meta: Any) -> tuple[str, str]:
    """Return the media type of a response and the charset of its text, ``utf-8`` when it declares none."""
    if isinstance(meta, int):
        return _TAG_MEDIA_TYPES[meta], "utf-8"
    response_type, custom_content_type = meta[0], meta[1]
    if not custom_content_type:
        # Rust sends each other response type as application/octet-stream.
        return _TYPE_MEDIA_TYPES.get(response_type, "application/octet-stream"), "utf-8"
    media_type, *parameters = custom_content_type.split(";")
    charset = "utf-8"
    for parameter in parameters:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip('"')
    return media_type.strip().lower(), charset


def route_tool_result(name: str, output: tuple[int, msgspec.json.Decoder] | None, wire: ResponseWireV1) -> ToolResult:
    """Map the HTTP response of an exposed route to its tool result.

    ``wire`` is the response that Bolt serializes for the route, with its ``response_model``
    and its response validation. A 2xx JSON body is the structured content, and its text is
    the HTTP body. A 2xx text body is text content only. Any other status raises ``ToolError``
    with the HTTP body. A tool result has no headers or cookies, so they are not sent.

    ``output`` is None when the tool has no output schema. Else it holds the status of the body
    that the output schema describes, and the decoder of the Struct of that schema. A success
    body that this decoder refuses raises ``TypeError``. Bolt does not validate the bytes of a
    ``Response``, so this check is necessary.
    """
    status, meta, body_kind, body = wire
    if body_kind != _BODY_BYTES:
        raise TypeError(f"Tool {name!r} returned a streaming or file response. A tool result cannot hold one.")
    if not 200 <= status < 300:
        raise ToolError(body.decode(errors="replace") if body else f"Tool {name!r} answered with HTTP status {status}.")
    media_type, charset = _content_type(meta)
    if output is not None:
        schema_status, decoder = output
        if status != schema_status or not _is_json_media_type(media_type):
            raise TypeError(
                f"Tool {name!r} returned a body that its output schema does not describe "
                f"(status {status}, {media_type!r})."
            )
        try:
            decoder.decode(body)
        except msgspec.DecodeError as exc:
            raise TypeError(f"Tool {name!r} returned a body that its output schema does not describe: {exc}") from exc
    if not body:
        return ToolResult(content=[])
    if _is_json_media_type(media_type):
        payload = json_decode(body)
        return ToolResult(
            content=body.decode(), structured=payload if isinstance(payload, dict) else {"result": payload}
        )
    if media_type.startswith("text/"):
        return ToolResult(content=body.decode(charset))
    raise TypeError(f"Tool {name!r} returned a body of type {media_type!r}. A tool result holds only JSON or text.")


async def call_route_async(
    handler: Callable[..., Awaitable[Any]],
    serialize: Callable[[Any], Awaitable[ResponseWireV1]],
    to_result: Callable[[ResponseWireV1], ToolResult],
    /,
    **kwargs: Any,
) -> ToolResult:
    """Call an async route handler and serialize its result as HTTP does."""
    return to_result(await serialize(await handler(**kwargs)))


def call_route_sync(
    handler: Callable[..., Any],
    serialize: Callable[[Any], ResponseWireV1],
    to_result: Callable[[ResponseWireV1], ToolResult],
    /,
    **kwargs: Any,
) -> ToolResult:
    """Call a sync route handler and serialize its result as HTTP does, on the same thread."""
    return to_result(serialize(handler(**kwargs)))


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
    except Exception as exc:
        return tool_failed(tool, exc, mask_error_details=mask_error_details)


def tool_failed(tool: ToolDef, exc: Exception, *, mask_error_details: bool) -> dict[str, Any]:
    """Map an exception from a tool or its middleware to an error result."""
    if isinstance(exc, (ToolError, McpClientError)):
        return error_result(str(exc))
    logger.exception("MCP tool %r failed", tool.name)
    return error_result(f"Tool {tool.name!r} failed." if mask_error_details else str(exc))
