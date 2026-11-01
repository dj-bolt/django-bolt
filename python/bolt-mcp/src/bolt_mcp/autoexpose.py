"""Expose existing django-bolt endpoints as MCP tools.

``expose_as_tool`` marks a handler; ``expose_routes`` walks a BoltAPI, reads each
marked route's pre-computed ``FieldDefinition`` metadata, and registers an MCP tool
that calls the original handler.
"""

from __future__ import annotations

import fnmatch
import functools
import inspect
import re
from collections.abc import Callable, Iterable
from typing import Any

from django_bolt.responses import HTML, JSON, PlainText, Response
from django_bolt.serialization import _RESPONSE_INSTANCE_TYPES, _compile_response_validator

from . import schema
from .registry import ToolDef
from .server import MCP

_MARKER_ATTR = "__bolt_mcp__"
_SKIP_SOURCES = frozenset({"file", "form"})


def expose_as_tool(name: str | None = None, description: str | None = None):
    """Mark a BoltAPI handler so ``expose_routes`` turns it into an MCP tool."""

    def decorator(fn: Callable) -> Callable:
        setattr(fn, _MARKER_ATTR, {"name": name, "description": description})
        return fn

    return decorator


def _slug(method: str, path: str) -> str:
    return f"{method.lower()}_{re.sub(r'[^a-zA-Z0-9]+', '_', path).strip('_')}"


def _name(handler: Callable) -> str:
    return getattr(handler, "__name__", repr(handler))


def _tool_name(handler: Callable, method: str, path: str) -> str:
    """Default MCP tool name: the handler's function name, else a path slug."""
    name = getattr(handler, "__name__", None)
    return name if name and not name.startswith("<") else _slug(method, path)


def _validator(meta: dict[str, Any], is_async: bool) -> Callable | None:
    """Return the validator that the HTTP route applies for ``meta``, or None."""
    validator_sync, validator_async = _compile_response_validator(meta)
    return validator_async if is_async else validator_sync


def _response_body(result: Any, validate: Callable | None, status: int) -> tuple[Any, Callable | None]:
    """Return the body of ``result`` and the validator that the HTTP route applies to it.

    The HTTP route validates the data of ``JSON`` and ``Response`` only when its
    status is ``status``, the status of the response model. A response object
    with no JSON body, for example a file or a stream, stays as it is.
    """
    if isinstance(result, JSON):
        return result.data, validate if result.status_code == status else None
    if isinstance(result, Response):
        content = result.content
        if result.status_code != status or isinstance(content, (bytes, bytearray, memoryview)):
            return content, None
        return content, validate
    if isinstance(result, PlainText):
        return result.text, None
    if isinstance(result, HTML):
        return result.html, None
    if isinstance(result, _RESPONSE_INSTANCE_TYPES):
        return result, None
    return result, validate


def _compile_body_picker(meta: dict[str, Any], is_async: bool) -> Callable[[Any], tuple[int | None, Any, Any]]:
    """Return a function that gives ``(status, body, validator)`` for a handler result.

    The function selects the response model as the HTTP route does. ``status``
    is the code of a ``(status, data)`` tuple, else None.
    """
    if not meta.get("is_multi_response"):
        validate = _validator(meta, is_async)
        default_status = meta["default_status_code"]

        def pick_single(result: Any) -> tuple[int | None, Any, Any]:
            return (None, *_response_body(result, validate, default_status))

        return pick_single

    # Each entry: (validator, status of the response model, the entry sends no body).
    entries = {
        code: (_validator(entry, is_async), entry["default_status_code"], entry["response_type"] is None)
        for code, entry in meta["_resolved_metas"].items()
    }
    catch_all = entries[...]
    default_validate, default_status, _ = entries.get(meta["default_status_code"], catch_all)

    def pick_multi(result: Any) -> tuple[int | None, Any, Any]:
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            code, data = result
            validate, _, no_body = entries.get(code, catch_all)
            if no_body or data is None:
                return code, None, None
            return code, data, validate
        if isinstance(result, JSON):
            validate, status, _ = entries.get(result.status_code, catch_all)
            return (None, *_response_body(result, validate, status))
        return (None, *_response_body(result, default_validate, default_status))

    return pick_multi


def _with_response_model(handler: Callable, meta: dict[str, Any]) -> Callable:
    """Return a wrapper that gives the body that the HTTP route sends for the result of ``handler``.

    The wrapper takes the data out of a ``(status, data)`` tuple or a response
    object. It then applies the response model of that status, as the HTTP
    route does. For example, a Serializer loses its write-only fields. A tuple
    gives ``[status, body]``.
    """
    is_async = inspect.iscoroutinefunction(handler)
    pick = _compile_body_picker(meta, is_async)
    if is_async:

        @functools.wraps(handler)
        async def filtered_async(**kwargs: Any) -> Any:
            status, body, validate = pick(await handler(**kwargs))
            if validate is not None:
                body = await validate(body)
            return body if status is None else [status, body]

        return filtered_async

    @functools.wraps(handler)
    def filtered(**kwargs: Any) -> Any:
        status, body, validate = pick(handler(**kwargs))
        if validate is not None:
            body = validate(body)
        return body if status is None else [status, body]

    return filtered


def expose_routes(
    mcp: MCP,
    api: Any,
    *,
    handlers: Iterable[Callable] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    methods: tuple[str, ...] = ("GET", "POST"),
    only_marked: bool = True,
) -> None:
    """Synthesize MCP tools from selected BoltAPI routes.

    By default every ``@expose_as_tool``-marked GET/POST route becomes a tool.

    ``handlers`` is an explicit allowlist of route handler callables. When given, only
    those handlers are exposed — regardless of HTTP method or marker (naming a handler
    is intent enough) — and ``include``/``exclude``/``methods``/``only_marked`` are
    ignored. A listed handler that isn't a route on ``api``, or that takes file/form
    parameters (which can't be represented as JSON tool arguments), raises ``ValueError``
    rather than being silently skipped.
    """
    explicit = handlers is not None
    # Materialize once — `handlers` may be a generator, and we iterate it twice
    # (to build `allow` here, and to compute `missing` after the route walk).
    handler_list = list(handlers) if handlers is not None else []
    allow = {id(h) for h in handler_list}
    matched: set[int] = set()
    wanted = {m.upper() for m in methods}

    for method, path, handler_id, handler in api._routes:
        if explicit:
            if id(handler) not in allow:
                continue
            matched.add(id(handler))
        else:
            if method.upper() not in wanted:
                continue
            if include and not any(fnmatch.fnmatch(path, pat) for pat in include):
                continue
            if exclude and any(fnmatch.fnmatch(path, pat) for pat in exclude):
                continue

        marker = getattr(handler, _MARKER_ATTR, None)
        if not explicit and only_marked and marker is None:
            continue

        meta = api._handler_meta.get(handler_id) or {}
        fields = meta.get("fields") or []
        unsupported = sorted({f.source for f in fields if f.source in _SKIP_SOURCES})
        if unsupported:
            if explicit:
                raise ValueError(
                    f"Cannot expose handler {_name(handler)!r} as an MCP tool: it uses "
                    f"unsupported parameter source(s) {unsupported} — file/form uploads "
                    f"can't be represented as JSON tool arguments."
                )
            continue

        marker = marker or {}
        # Name/description derive from the route itself; @expose_as_tool only overrides.
        name = marker.get("name") or _tool_name(handler, method, path)
        if name in mcp._tools:
            raise ValueError(
                f"MCP tool name {name!r} is already registered — two exposed routes (or a "
                f"route and a native tool) resolve to the same name. Disambiguate with "
                f"@expose_as_tool(name=...)."
            )
        description = (
            marker.get("description")
            or meta.get("openapi_description")
            or inspect.getdoc(handler)
            or meta.get("openapi_summary")
        )
        args_struct = schema.struct_from_fields(name, fields)
        mcp.add_tool(
            ToolDef(
                name=name,
                fn=_with_response_model(handler, meta) if meta else handler,
                description=description,
                args_struct=args_struct,
                input_schema=schema.input_schema_for(args_struct),
                is_async=inspect.iscoroutinefunction(handler),
                injects_request=any(f.source == "request" for f in fields),
            )
        )

    if explicit:
        missing = [h for h in handler_list if id(h) not in matched]
        if missing:
            names = ", ".join(_name(h) for h in missing)
            raise ValueError(f"Handler(s) not registered as a route on this BoltAPI: {names}")
