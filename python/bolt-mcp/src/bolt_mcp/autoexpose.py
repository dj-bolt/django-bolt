"""Expose existing django-bolt endpoints as MCP tools.

``expose_as_tool`` marks a handler; ``expose_routes`` walks a BoltAPI, reads each
marked route's pre-computed ``FieldDefinition`` metadata, and registers an MCP tool
that calls the original handler.
"""

from __future__ import annotations

import fnmatch
import inspect
import re
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any

from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack

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


def _middleware_names(spec: Any) -> list[str]:
    """Return the class names of one Python middleware spec, for an error message."""
    if isinstance(spec, DjangoMiddlewareStack):
        return [getattr(cls, "__name__", repr(cls)) for cls in spec.middleware_classes]
    if isinstance(spec, DjangoMiddleware):
        return [getattr(spec.middleware_class, "__name__", repr(spec.middleware_class))]
    return [spec.__name__ if isinstance(spec, type) else type(spec).__name__]


def _route_auth(
    handler: Callable, method: str, path: str, route_meta: dict[str, Any], meta: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the auth and the guards of a route for its tool, or None for a route with neither.

    Rust runs them for each call of the tool, as for an HTTP request to the route.
    A tool call cannot run middleware, so a route with middleware that can refuse a request raises ``ValueError``.
    """
    blocking = [mw["type"] for mw in route_meta.get("middleware", ()) if mw.get("type") == "rate_limit"]
    middleware_api = meta.get("_middleware_api")
    if middleware_api is not None:
        # The global middleware that the route runs over HTTP: None means all of the API, after @skip_middleware.
        global_middleware = meta.get("_global_middleware")
        if global_middleware is None:
            global_middleware = middleware_api._middleware
        specs = [*global_middleware, *meta.get("_router_middleware", ()), *meta.get("_route_middleware", ())]
        blocking += [name for spec in specs if not isinstance(spec, dict) for name in _middleware_names(spec)]
    if blocking:
        raise ValueError(
            f"Cannot expose handler {_name(handler)!r} ({method} {path}) as an MCP tool: "
            f"a tool call cannot run the middleware of its route ({', '.join(blocking)}). "
            f"Write an @mcp.tool(guards=...) for this operation."
        )
    backends = route_meta.get("auth_backends", [])
    guards = route_meta.get("guards", [])
    if not backends and not guards:
        return None
    return {"auth_backends": backends, "guards": guards}


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

        route_auth = _route_auth(handler, method, path, api._handler_middleware.get(handler_id) or {}, meta)
        # The revocation check of the route, as HTTP runs it. None when no backend of the route has revocation.
        revocation_handlers = meta.get("_revocation_handlers")
        revocation_check = (
            partial(api._check_revocation, revocation_handlers=revocation_handlers)
            if revocation_handlers is not None
            else None
        )

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
                fn=handler,
                description=description,
                args_struct=args_struct,
                input_schema=schema.input_schema_for(args_struct),
                is_async=inspect.iscoroutinefunction(handler),
                injects_request=any(f.source == "request" for f in fields),
                route_auth=route_auth,
                revocation_check=revocation_check,
            )
        )

    if explicit:
        missing = [h for h in handler_list if id(h) not in matched]
        if missing:
            names = ", ".join(_name(h) for h in missing)
            raise ValueError(f"Handler(s) not registered as a route on this BoltAPI: {names}")
