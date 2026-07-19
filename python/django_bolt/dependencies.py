"""Dependency injection utilities."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from .params import Depends as DependsMarker
from .typing import FieldDefinition

_POSITIONAL_KINDS = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)

# Source ids for the pre-compiled per-dependency argument plan.
# Pre-sorting at compile time removes per-request string dispatch on
# field.source (hot-path rule: no string dispatch in loops).
_SRC_REQUEST = 0
_SRC_HEADER = 1
_SRC_COOKIE = 2
_SRC_OTHER = 3


def _compile_dep_arg_plan(dep_meta: dict[str, Any]) -> list[tuple[int, str, str, bool, str]]:
    """Compile the per-field extraction plan for a dependency ONCE.

    Each entry is (source_id, lookup_key, lookup_key_lower, positional, arg_name).
    Cached in dep_meta so every subsequent request iterates plain tuples.
    """
    plan: list[tuple[int, str, str, bool, str]] = []
    for field in dep_meta["fields"]:
        key = field.alias or field.name
        positional = field.kind in _POSITIONAL_KINDS
        source = field.source
        if source == "request":
            src = _SRC_REQUEST
        elif source == "header":
            src = _SRC_HEADER
        elif source == "cookie":
            src = _SRC_COOKIE
        else:
            src = _SRC_OTHER
        plan.append((src, key, key.lower(), positional, field.name))
    dep_meta["_dep_arg_plan"] = plan
    return plan


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

    dep_meta = handler_meta.get(dep_fn)
    if dep_meta is None:
        # Compile dependency metadata with the actual HTTP method and path
        # Dependencies MUST be validated against HTTP method constraints
        # e.g., a dependency with Body() can't be used in GET handlers
        dep_meta = compile_binder(dep_fn, http_method, path)
        handler_meta[dep_fn] = dep_meta

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
        args, kwargs = _extract_dependency_args(dep_meta, request, params_map, query_map, headers_map, cookies_map)
        if is_async:
            value = await dep_fn(*args, **kwargs)
        else:
            value = dep_fn(*args, **kwargs)

    if depends_marker.use_cache:
        dep_cache[dep_fn] = value

    return value


def _extract_dependency_args(
    dep_meta: dict[str, Any],
    request: dict[str, Any],
    params_map: dict[str, Any],
    query_map: dict[str, Any],
    headers_map: dict[str, str],
    cookies_map: dict[str, str],
) -> tuple[list[Any], dict[str, Any]]:
    """Shared parameter extraction for async and sync dependency calls.

    Iterates the pre-compiled plan — no per-field string dispatch, no
    ``inspect.Parameter`` kind checks at request time.
    """
    plan = dep_meta.get("_dep_arg_plan")
    if plan is None:
        plan = _compile_dep_arg_plan(dep_meta)

    dep_args: list[Any] = []
    dep_kwargs: dict[str, Any] = {}

    for src, key, key_lower, positional, name in plan:
        # Rust pre-converts values to typed Python objects (int, float, bool, str)
        if src == _SRC_REQUEST:
            dval = request
        elif key in params_map:
            dval = params_map[key]
        elif key in query_map:
            dval = query_map[key]
        elif src == _SRC_HEADER:
            dval = headers_map.get(key_lower)
            if dval is None:
                raise ValueError(f"Missing required header: {key}")
        elif src == _SRC_COOKIE:
            dval = cookies_map.get(key)
            if dval is None:
                raise ValueError(f"Missing required cookie: {key}")
        else:
            dval = None

        if positional:
            dep_args.append(dval)
        else:
            dep_kwargs[name] = dval

    return dep_args, dep_kwargs


async def call_dependency(
    dep_fn: Callable,
    dep_meta: dict[str, Any],
    request: dict[str, Any],
    params_map: dict[str, Any],
    query_map: dict[str, Any],
    headers_map: dict[str, str],
    cookies_map: dict[str, str],
) -> Any:
    """Call an async dependency function with resolved parameters."""
    args, kwargs = _extract_dependency_args(dep_meta, request, params_map, query_map, headers_map, cookies_map)
    return await dep_fn(*args, **kwargs)


def call_dependency_sync(
    dep_fn: Callable,
    dep_meta: dict[str, Any],
    request: dict[str, Any],
    params_map: dict[str, Any],
    query_map: dict[str, Any],
    headers_map: dict[str, str],
    cookies_map: dict[str, str],
) -> Any:
    """Call a sync dependency function with resolved parameters."""
    args, kwargs = _extract_dependency_args(dep_meta, request, params_map, query_map, headers_map, cookies_map)
    return dep_fn(*args, **kwargs)


def extract_dependency_value(
    field: FieldDefinition,
    params_map: dict[str, Any],
    query_map: dict[str, Any],
    headers_map: dict[str, str],
    cookies_map: dict[str, str],
) -> Any:
    """Extract value for a dependency parameter using FieldDefinition.

    Kept for backward compatibility — the request hot path uses the
    pre-compiled plan in :func:`_extract_dependency_args` instead.
    """
    key = field.alias or field.name

    # Rust pre-converts values to typed Python objects (int, float, bool, str)
    if key in params_map:
        return params_map[key]
    elif key in query_map:
        return query_map[key]
    elif field.source == "header":
        raw = headers_map.get(key.lower())
        if raw is None:
            raise ValueError(f"Missing required header: {key}")
        return raw
    elif field.source == "cookie":
        raw = cookies_map.get(key)
        if raw is None:
            raise ValueError(f"Missing required cookie: {key}")
        return raw
    else:
        return None
