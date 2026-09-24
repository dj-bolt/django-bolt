"""Dependency injection utilities."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from .params import Depends as DependsMarker

_POSITIONAL_KINDS = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)

# Source ids for the pre-compiled per-dependency argument plan.
# Pre-sorting at compile time removes per-request string dispatch on
# field.source (hot-path rule: no string dispatch in loops).
_SRC_REQUEST = 0
_SRC_PATH = 1
_SRC_QUERY = 2
_SRC_HEADER = 3
_SRC_COOKIE = 4
_SRC_DEPENDENCY = 5
_SRC_OTHER = 6

_SOURCE_IDS = {
    "request": _SRC_REQUEST,
    "path": _SRC_PATH,
    "query": _SRC_QUERY,
    "header": _SRC_HEADER,
    "cookie": _SRC_COOKIE,
    "dependency": _SRC_DEPENDENCY,
}


def _compile_dep_arg_plan(dep_meta: dict[str, Any]) -> list[tuple[int, Any, bool, str]]:
    """Compile the per-field extraction plan for a dependency ONCE.

    Each entry is (source_id, extractor, positional, arg_name). A path, query,
    header or cookie field uses the extractor of its field, as a handler does.
    So names, aliases, defaults and missing-value errors are the same as for a
    handler. The markers of nested dependencies go to ``_dep_nested``.
    Both are cached in dep_meta, so each request iterates plain tuples.
    """
    plan: list[tuple[int, Any, bool, str]] = []
    nested: list[tuple[str, DependsMarker]] = []
    for field in dep_meta["fields"]:
        src = _SOURCE_IDS.get(field.source, _SRC_OTHER)
        if src == _SRC_DEPENDENCY:
            if field.dependency is None:
                raise ValueError(f"Depends for parameter {field.name} requires a callable")
            nested.append((field.name, field.dependency))
        plan.append((src, field.extractor, field.kind in _POSITIONAL_KINDS, field.name))
    dep_meta["_dep_nested"] = nested
    dep_meta["_dep_arg_plan"] = plan
    return plan


def dependency_needs_event_loop(
    dep_fn: Callable,
    handler_meta: dict[Callable, dict[str, Any]],
    compile_binder: Callable,
    http_method: str,
    path: str,
) -> bool:
    """Whether ``dep_fn``, or a dependency that it depends on, is async.

    The sync injector can resolve a dependency only when this is false.
    """
    if inspect.iscoroutinefunction(dep_fn):
        return True
    dep_meta = _dep_meta(dep_fn, handler_meta, compile_binder, http_method, path)
    if "_dep_arg_plan" not in dep_meta:
        _compile_dep_arg_plan(dep_meta)
    return any(
        dependency_needs_event_loop(marker.dependency, handler_meta, compile_binder, http_method, path)
        for _, marker in dep_meta["_dep_nested"]
    )


_TYPED_PARAM_SOURCES = ("path", "query", "header", "cookie")


def dependency_param_fields(meta: dict[str, Any], compile_dep_fn: Callable[[Callable], dict[str, Any]]) -> list[Any]:
    """The path, query, header and cookie fields of each dependency in the tree of ``meta``.

    Rust converts the values of these fields to their types, as it does for the
    fields of the handler. Each dependency is visited one time.
    """
    fields: list[Any] = []
    visited: set[int] = set()
    pending = [meta]
    while pending:
        current = pending.pop()
        for field in current["fields"]:
            if field.source != "dependency" or field.dependency is None:
                continue
            dep_fn = field.dependency.dependency
            if id(dep_fn) in visited:
                continue
            visited.add(id(dep_fn))
            dep_meta = compile_dep_fn(dep_fn)
            fields.extend(f for f in dep_meta["fields"] if f.source in _TYPED_PARAM_SOURCES)
            pending.append(dep_meta)
    return fields


def _dep_meta(
    dep_fn: Callable,
    handler_meta: dict[Callable, dict[str, Any]],
    compile_binder: Callable,
    http_method: str,
    path: str,
) -> dict[str, Any]:
    dep_meta = handler_meta.get(dep_fn)
    if dep_meta is None:
        # Compile dependency metadata with the actual HTTP method and path
        # Dependencies MUST be validated against HTTP method constraints
        # e.g., a dependency with Body() can't be used in GET handlers
        dep_meta = compile_binder(dep_fn, http_method, path)
        handler_meta[dep_fn] = dep_meta
    return dep_meta


async def resolve_dependency(
    dep_fn: Callable,
    depends_marker: DependsMarker,
    request: dict[str, Any],
    dep_cache: dict[Any, Any],
    params_map: dict[str, Any],
    query_map: dict[str, Any],
    headers_map: dict[str, str],
    cookies_map: dict[str, str],
    handler_meta: dict[Callable, dict[str, Any]],
    compile_binder: Callable,
    http_method: str,
    path: str,
) -> Any:
    """
    Resolve a dependency injection.

    Args:
        dep_fn: Dependency function to resolve
        depends_marker: Depends marker with cache settings
        request: Request dict
        dep_cache: Cache for resolved dependencies
        params_map: Path parameters
        query_map: Query parameters
        headers_map: Request headers
        cookies_map: Request cookies
        handler_meta: Metadata cache for handlers
        compile_binder: Function to compile parameter binding metadata
        http_method: HTTP method of the handler using this dependency
        path: Path of the handler using this dependency

    Returns:
        Resolved dependency value
    """
    if depends_marker.use_cache and dep_fn in dep_cache:
        return dep_cache[dep_fn]

    dep_meta = _dep_meta(dep_fn, handler_meta, compile_binder, http_method, path)

    # Cache async/request-only checks once per dependency metadata (hot path)
    is_async = dep_meta.get("_is_async_dep")
    if is_async is None:
        is_async = inspect.iscoroutinefunction(dep_fn)
        dep_meta["_is_async_dep"] = is_async
        dep_meta["_is_request_only_dep"] = dep_meta.get("mode") == "request_only"

    if dep_meta["_is_request_only_dep"]:
        if is_async:
            value = await dep_fn(request)
        else:
            value = dep_fn(request)
    else:
        plan = dep_meta.get("_dep_arg_plan")
        if plan is None:
            plan = _compile_dep_arg_plan(dep_meta)
        nested_values = None
        if dep_meta["_dep_nested"]:
            nested_values = {}
            for name, marker in dep_meta["_dep_nested"]:
                nested_values[name] = await resolve_dependency(
                    marker.dependency,
                    marker,
                    request,
                    dep_cache,
                    params_map,
                    query_map,
                    headers_map,
                    cookies_map,
                    handler_meta,
                    compile_binder,
                    http_method,
                    path,
                )
        args, kwargs = _bind_dependency_args(
            plan, request, params_map, query_map, headers_map, cookies_map, nested_values
        )
        if is_async:
            value = await dep_fn(*args, **kwargs)
        else:
            value = dep_fn(*args, **kwargs)

    if depends_marker.use_cache:
        dep_cache[dep_fn] = value

    return value


def resolve_dependency_sync(
    dep_fn: Callable,
    depends_marker: DependsMarker,
    request: dict[str, Any],
    dep_cache: dict[Any, Any],
    params_map: dict[str, Any],
    query_map: dict[str, Any],
    headers_map: dict[str, str],
    cookies_map: dict[str, str],
    handler_meta: dict[Callable, dict[str, Any]],
    compile_binder: Callable,
    http_method: str,
    path: str,
) -> Any:
    """Sync form of :func:`resolve_dependency`, for a sync dependency of a sync handler.

    Its injector then needs no event loop, so the handler keeps sync dispatch.
    """
    if depends_marker.use_cache and dep_fn in dep_cache:
        return dep_cache[dep_fn]

    dep_meta = _dep_meta(dep_fn, handler_meta, compile_binder, http_method, path)

    if dep_meta.get("mode") == "request_only":
        value = dep_fn(request)
    else:
        plan = dep_meta.get("_dep_arg_plan")
        if plan is None:
            plan = _compile_dep_arg_plan(dep_meta)
        nested_values = None
        if dep_meta["_dep_nested"]:
            nested_values = {}
            for name, marker in dep_meta["_dep_nested"]:
                nested_values[name] = resolve_dependency_sync(
                    marker.dependency,
                    marker,
                    request,
                    dep_cache,
                    params_map,
                    query_map,
                    headers_map,
                    cookies_map,
                    handler_meta,
                    compile_binder,
                    http_method,
                    path,
                )
        args, kwargs = _bind_dependency_args(
            plan, request, params_map, query_map, headers_map, cookies_map, nested_values
        )
        value = dep_fn(*args, **kwargs)

    if depends_marker.use_cache:
        dep_cache[dep_fn] = value

    return value


def _bind_dependency_args(
    plan: list[tuple[int, Any, bool, str]],
    request: dict[str, Any],
    params_map: dict[str, Any],
    query_map: dict[str, Any],
    headers_map: dict[str, str],
    cookies_map: dict[str, str],
    nested_values: dict[str, Any] | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Bind the arguments of a dependency call from its pre-compiled plan.

    ``nested_values`` holds the resolved nested dependencies by argument name.
    """
    dep_args: list[Any] = []
    dep_kwargs: dict[str, Any] = {}

    for src, extractor, positional, name in plan:
        # Rust pre-converts values to typed Python objects (int, float, bool, str)
        if src == _SRC_REQUEST:
            dval = request
        elif src == _SRC_PATH:
            dval = extractor(params_map)
        elif src == _SRC_QUERY:
            dval = extractor(query_map)
        elif src == _SRC_HEADER:
            dval = extractor(headers_map)
        elif src == _SRC_COOKIE:
            dval = extractor(cookies_map)
        elif src == _SRC_DEPENDENCY:
            dval = nested_values[name]
        else:
            dval = None

        if positional:
            dep_args.append(dval)
        else:
            dep_kwargs[name] = dval

    return dep_args, dep_kwargs
