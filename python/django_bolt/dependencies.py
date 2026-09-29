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
_SRC_BODY = 6
_SRC_FORM = 7
_SRC_FORM_WITH_FILES = 8
_SRC_FILE = 9

_SOURCE_IDS = {
    "request": _SRC_REQUEST,
    "path": _SRC_PATH,
    "query": _SRC_QUERY,
    "header": _SRC_HEADER,
    "cookie": _SRC_COOKIE,
    "dependency": _SRC_DEPENDENCY,
    "body": _SRC_BODY,
    "form": _SRC_FORM,
    "file": _SRC_FILE,
}


def compile_dependency(
    dep_fn: Callable,
    handler_meta: dict[Any, dict[str, Any]],
    compile_binder: Callable,
    http_method: str,
    path: str,
) -> dict[str, Any]:
    """Compile the binding of ``dep_fn`` for one route, and of each dependency that it depends on.

    Call this at registration. The result goes to :func:`resolve_dependency`
    at run time, so a request does not look up or compile a binding.
    """
    dep_meta = _dep_meta(dep_fn, handler_meta, compile_binder, http_method, path)
    if "_dep_arg_plan" not in dep_meta:
        _compile_dep_arg_plan(dep_fn, dep_meta, handler_meta, compile_binder)
    return dep_meta


def _compile_dep_arg_plan(
    dep_fn: Callable,
    dep_meta: dict[str, Any],
    handler_meta: dict[Any, dict[str, Any]],
    compile_binder: Callable,
) -> None:
    """Compile the per-field extraction plan for a dependency ONCE.

    Each entry is (source_id, extractor, positional, arg_name). Each field
    uses the extractor of its field, as a handler does. So names, aliases,
    defaults and missing-value errors are the same as for a handler. Each
    nested dependency goes to ``_dep_nested`` as (arg_name, marker, dep_meta),
    and its sync form goes to ``_dep_nested_sync``. All of this is cached in
    dep_meta, so each request iterates plain tuples.
    """
    plan: list[tuple[int, Any, bool, str]] = []
    nested_markers: list[tuple[str, DependsMarker, DependsMarker]] = []
    for field in dep_meta["fields"]:
        src = _SOURCE_IDS.get(field.source)
        if src is None:
            raise TypeError(f"Dependency parameter {field.name!r} has an unsupported source {field.source!r}")
        if src == _SRC_DEPENDENCY:
            if field.dependency is None:
                raise ValueError(f"Depends for parameter {field.name} requires a callable")
            nested_markers.append((field.name, field.dependency, sync_form(field.dependency)))
        elif src == _SRC_FORM and getattr(field.extractor, "needs_files_map", False):
            src = _SRC_FORM_WITH_FILES
        plan.append((src, field.extractor, field.kind in _POSITIONAL_KINDS, field.name))
    nested: list[tuple[str, DependsMarker, dict[str, Any]]] = []
    nested_sync: list[tuple[str, DependsMarker, dict[str, Any]]] = []
    http_method = dep_meta["http_method"]
    path = dep_meta["path"]
    for name, marker, sync_marker in nested_markers:
        nested.append(
            (name, marker, compile_dependency(marker.dependency, handler_meta, compile_binder, http_method, path))
        )
        nested_sync.append(
            (
                name,
                sync_marker,
                compile_dependency(sync_marker.dependency, handler_meta, compile_binder, http_method, path),
            )
        )
    dep_meta["_is_async_dep"] = inspect.iscoroutinefunction(dep_fn)
    dep_meta["_is_request_only_dep"] = dep_meta.get("mode") == "request_only"
    dep_meta["_dep_nested"] = nested
    dep_meta["_dep_nested_sync"] = nested_sync
    # Set the plan last, because a binding with a plan is complete. An error in
    # a nested dependency then leaves no part of this binding in the cache. A
    # cycle of dependencies raises RecursionError at registration.
    dep_meta["_dep_arg_plan"] = plan


def sync_form(marker: DependsMarker) -> DependsMarker:
    """The marker of the sync form of a dependency, for example ``get_current_user_sync``.

    A dependency with no ``_bolt_sync_variant`` keeps its marker.
    """
    sync_variant = getattr(marker.dependency, "_bolt_sync_variant", None)
    if sync_variant is None:
        return marker
    return DependsMarker(dependency=sync_variant, use_cache=marker.use_cache)


def dependency_needs_event_loop(
    dep_fn: Callable,
    handler_meta: dict[Callable, dict[str, Any]],
    compile_binder: Callable,
    http_method: str,
    path: str,
    *,
    sync_handler: bool,
) -> bool:
    """Whether ``dep_fn``, or a dependency that it depends on, is async.

    The sync injector can resolve a dependency only when this is false. For a
    sync handler, a nested dependency counts in its sync form, as
    :func:`resolve_dependency_sync` resolves it.
    """
    if inspect.iscoroutinefunction(dep_fn):
        return True
    dep_meta = compile_dependency(dep_fn, handler_meta, compile_binder, http_method, path)
    nested = dep_meta["_dep_nested_sync"] if sync_handler else dep_meta["_dep_nested"]
    return any(
        dependency_needs_event_loop(
            marker.dependency, handler_meta, compile_binder, http_method, path, sync_handler=sync_handler
        )
        for _, marker, _ in nested
    )


_REQUEST_DATA_SOURCES = frozenset(("path", "query", "header", "cookie", "body", "form", "file"))


def dependency_fields(meta: dict[str, Any], compile_dep_fn: Callable[[Callable], dict[str, Any]]) -> list[Any]:
    """The request data fields of each dependency in the tree of ``meta``.

    These are the path, query, header, cookie, body, form and file fields. The
    route uses them with the fields of its handler: for its parsing flags, for
    the Rust metadata, and for its OpenAPI schema. Each dependency is visited
    one time.
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
            fields.extend(f for f in dep_meta["fields"] if f.source in _REQUEST_DATA_SOURCES)
            pending.append(dep_meta)
    return fields


_NOT_DECODED = object()


def decode_body_once(request_cache: dict[Any, Any], extractor: Callable, body: bytes) -> Any:
    """Decode the request body one time for each body type in a request.

    There is one body extractor for each type, so it is the cache key. A
    handler and its dependencies that take the same type share one object.
    ``request_cache`` is the dependency cache of the request.
    """
    value = request_cache.get(extractor, _NOT_DECODED)
    if value is _NOT_DECODED:
        value = request_cache[extractor] = extractor(body)
    return value


def _dep_meta(
    dep_fn: Callable,
    handler_meta: dict[Callable, dict[str, Any]],
    compile_binder: Callable,
    http_method: str,
    path: str,
) -> dict[str, Any]:
    # The route sets the source of a field: a name in the path is a path
    # parameter on one route and a query parameter on another. The method
    # sets whether a body is allowed. So the binding is cached for each route.
    key = (dep_fn, http_method, path)
    dep_meta = handler_meta.get(key)
    if dep_meta is None:
        dep_meta = compile_binder(dep_fn, http_method, path)
        handler_meta[key] = dep_meta
    return dep_meta


async def resolve_dependency(
    dep_fn: Callable,
    depends_marker: DependsMarker,
    dep_meta: dict[str, Any],
    request: dict[str, Any],
    dep_cache: dict[Any, Any],
    params_map: dict[str, Any],
    query_map: dict[str, Any],
    headers_map: dict[str, str],
    cookies_map: dict[str, str],
) -> Any:
    """
    Resolve a dependency injection.

    Args:
        dep_fn: Dependency function to resolve
        depends_marker: Depends marker with cache settings
        dep_meta: Binding of ``dep_fn`` for this route, from :func:`compile_dependency`
        request: Request dict
        dep_cache: Cache for resolved dependencies
        params_map: Path parameters
        query_map: Query parameters
        headers_map: Request headers
        cookies_map: Request cookies

    Returns:
        Resolved dependency value
    """
    if depends_marker.use_cache and dep_fn in dep_cache:
        return dep_cache[dep_fn]

    if dep_meta["_is_request_only_dep"]:
        if dep_meta["_is_async_dep"]:
            value = await dep_fn(request)
        else:
            value = dep_fn(request)
    else:
        nested_values = None
        if dep_meta["_dep_nested"]:
            nested_values = {}
            for name, marker, nested_meta in dep_meta["_dep_nested"]:
                nested_values[name] = await resolve_dependency(
                    marker.dependency,
                    marker,
                    nested_meta,
                    request,
                    dep_cache,
                    params_map,
                    query_map,
                    headers_map,
                    cookies_map,
                )
        args, kwargs = _bind_dependency_args(
            dep_meta["_dep_arg_plan"],
            request,
            dep_cache,
            params_map,
            query_map,
            headers_map,
            cookies_map,
            nested_values,
        )
        if dep_meta["_is_async_dep"]:
            value = await dep_fn(*args, **kwargs)
        else:
            value = dep_fn(*args, **kwargs)

    if depends_marker.use_cache:
        dep_cache[dep_fn] = value

    return value


def resolve_dependency_sync(
    dep_fn: Callable,
    depends_marker: DependsMarker,
    dep_meta: dict[str, Any],
    request: dict[str, Any],
    dep_cache: dict[Any, Any],
    params_map: dict[str, Any],
    query_map: dict[str, Any],
    headers_map: dict[str, str],
    cookies_map: dict[str, str],
) -> Any:
    """Sync form of :func:`resolve_dependency`, for a sync dependency of a sync handler.

    Its injector then needs no event loop, so the handler keeps sync dispatch.
    """
    if depends_marker.use_cache and dep_fn in dep_cache:
        return dep_cache[dep_fn]

    if dep_meta["_is_request_only_dep"]:
        value = dep_fn(request)
    else:
        nested_values = None
        if dep_meta["_dep_nested_sync"]:
            nested_values = {}
            for name, marker, nested_meta in dep_meta["_dep_nested_sync"]:
                nested_values[name] = resolve_dependency_sync(
                    marker.dependency,
                    marker,
                    nested_meta,
                    request,
                    dep_cache,
                    params_map,
                    query_map,
                    headers_map,
                    cookies_map,
                )
        args, kwargs = _bind_dependency_args(
            dep_meta["_dep_arg_plan"],
            request,
            dep_cache,
            params_map,
            query_map,
            headers_map,
            cookies_map,
            nested_values,
        )
        value = dep_fn(*args, **kwargs)

    if depends_marker.use_cache:
        dep_cache[dep_fn] = value

    return value


def _bind_dependency_args(
    plan: list[tuple[int, Any, bool, str]],
    request: dict[str, Any],
    dep_cache: dict[Any, Any],
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
        elif src == _SRC_BODY:
            dval = decode_body_once(dep_cache, extractor, request["body"])
        elif src == _SRC_FORM:
            dval = extractor(request.form)
        elif src == _SRC_FORM_WITH_FILES:
            dval = extractor(request.form, request.files)
        else:
            dval = extractor(request.files)

        if positional:
            dep_args.append(dval)
        else:
            dep_kwargs[name] = dval

    return dep_args, dep_kwargs
