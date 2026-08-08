"""Python execution surface for the Rust MCP core.

Rust answers every catalog request itself and calls these async callables only
to *execute* tools, resources, and prompts. Each callable takes one payload
dict (built in Rust, every key always present) and returns wire-format result
JSON as ``bytes`` — or a tagged envelope:

- ``{"__inputRequired__": [{key, method, params}]}`` — an MRTR input point
  fired (2026-07-28 clients); Rust seals the requestState and builds the
  ``input_required`` result.
- ``{"__error__": {"kind": ..., "message": ...}}`` — a JSON-RPC-level error;
  Rust maps ``kind`` to the era-correct error code.
"""

from __future__ import annotations

import contextlib
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import msgspec

from django_bolt._json import decode as json_decode
from django_bolt._json import encode as json_encode

from ._execute import error_result, execute_tool
from .context import Context, _InputRequired

if TYPE_CHECKING:
    from .registry import ResourceDef, ResourceTemplateDef, ToolDef
    from .server import MCP

_KIND_METHOD = {"elicitation": "elicitation/create", "sampling": "sampling/createMessage"}


class McpRequest:
    """Minimal request stand-in handed to tools that declare ``request``.

    The HTTP request itself is consumed by the Rust transport; what tools
    actually need from it is the authenticated principal (``bolt_mcp.principal``
    reads ``.context``) and a mutable scratch ``state``.
    """

    __slots__ = ("context", "state")

    def __init__(self, context: dict[str, Any] | None) -> None:
        self.context = context
        self.state: dict[str, Any] = {}


async def _run_tool(tool: ToolDef, mcp: MCP, payload: dict[str, Any]) -> bytes:
    raw = json_decode(payload["arguments_json"])
    try:
        args = msgspec.convert(raw or {}, tool.args_struct)
    except msgspec.ValidationError as exc:
        return json_encode(error_result(f"Invalid arguments: {exc}"))
    kwargs = msgspec.structs.asdict(args)
    if tool.injects_request:
        kwargs["request"] = McpRequest(payload["auth"])
    if tool.ctx_param is not None:
        kwargs[tool.ctx_param] = Context(
            payload["ctx"],
            request=McpRequest(payload["auth"]),
            resolver=partial(_resolve_resource, mcp),
        )
    try:
        result = await execute_tool(tool, kwargs)
    except _InputRequired as pending:
        return json_encode(
            {
                "__inputRequired__": [
                    {
                        "key": pending.key,
                        "method": _KIND_METHOD[pending.kind],
                        "params": pending.params,
                    }
                ]
            }
        )
    return json_encode(result)


def tool_entry(mcp: MCP, tool: ToolDef) -> dict[str, Any]:
    """The per-tool config dict Rust stores in its registry."""
    return {
        "dispatch": partial(_run_tool, tool, mcp),
        "needs_context": tool.ctx_param is not None,
        "guards": [g.to_metadata() for g in tool.guards],
    }


async def _invoke(fn: Any, is_async: bool, /, **kwargs: Any) -> Any:
    return await fn(**kwargs) if is_async else fn(**kwargs)


async def _resolve_resource(mcp: MCP, uri: str) -> tuple[str, str]:
    """Resolve a URI to ``(text, mime_type)``: static resources, then templates.

    Raises ``LookupError`` for no match and ``msgspec.ValidationError`` when a
    template matches but its ``{vars}`` don't coerce to the handler's types.
    Shared by ``resources/read`` and ``Context.read_resource``.
    """
    resource: ResourceDef | None = mcp._resources.get(uri)
    if resource is not None:
        return await _invoke(resource.fn, resource.is_async), resource.mime_type
    tmpl: ResourceTemplateDef
    for tmpl in mcp._resource_templates.values():
        match = tmpl.pattern.match(uri)
        if match is None:
            continue
        raw = {k: unquote(v) for k, v in match.groupdict().items()}
        kwargs = msgspec.structs.asdict(msgspec.convert(raw, tmpl.args_struct, strict=False))
        return await _invoke(tmpl.fn, tmpl.is_async, **kwargs), tmpl.mime_type
    raise LookupError(uri)


def _error(kind: str, message: str) -> bytes:
    return json_encode({"__error__": {"kind": kind, "message": message}})


async def _read_resource(mcp: MCP, payload: dict[str, Any]) -> bytes:
    uri = payload["uri"]
    try:
        text, mime_type = await _resolve_resource(mcp, uri)
    except LookupError:
        return _error("resource_not_found", f"Unknown resource: {uri!r}")
    except msgspec.ValidationError as exc:
        return _error("invalid_params", f"Invalid resource URI {uri!r}: {exc}")
    return json_encode({"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]})


def resource_dispatch(mcp: MCP) -> Any:
    return partial(_read_resource, mcp)


async def _get_prompt(mcp: MCP, payload: dict[str, Any]) -> bytes:
    name = payload["name"]
    prompt = mcp._prompts.get(name)
    if prompt is None:
        return _error("invalid_params", f"Unknown prompt: {name!r}")
    raw = json_decode(payload["arguments_json"])
    try:
        args = msgspec.convert(raw or {}, prompt.args_struct)
    except msgspec.ValidationError as exc:
        return _error("invalid_params", f"Invalid arguments: {exc}")
    kwargs = msgspec.structs.asdict(args)
    rendered = await _invoke(prompt.fn, prompt.is_async, **kwargs)
    if isinstance(rendered, str):
        messages = [{"role": "user", "content": {"type": "text", "text": rendered}}]
    else:
        messages = rendered
    result: dict[str, Any] = {"messages": messages}
    if prompt.description:
        result["description"] = prompt.description
    return json_encode(result)


def prompt_dispatch(mcp: MCP) -> Any:
    return partial(_get_prompt, mcp)


def make_verify_token(oauth_resource: Any) -> Any:
    """Adapt a ``ProtectedResource`` to the flat principal contract Rust expects.

    Returns ``None`` for invalid tokens or insufficient scope; otherwise a dict
    with every key present: user_id / is_staff / is_superuser / permissions /
    claims_json / backend.
    """

    def verify(token: str) -> dict[str, Any] | None:
        claims = oauth_resource.token_verifier(token) if oauth_resource.token_verifier else None
        if claims is None:
            return None
        if oauth_resource.required_scopes:
            granted = set((claims.get("scope") or "").split())
            if not granted.issuperset(oauth_resource.required_scopes):
                return None
        claims_json: str | None = None
        with contextlib.suppress(Exception):
            claims_json = json_encode(claims).decode()
        return {
            "user_id": str(claims["sub"]) if claims.get("sub") is not None else None,
            "is_staff": bool(claims.get("is_staff")),
            "is_superuser": bool(claims.get("is_superuser")),
            "permissions": list(claims.get("permissions") or []),
            "claims_json": claims_json,
            "backend": "oauth",
        }

    return verify
