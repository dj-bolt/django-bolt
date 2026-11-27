"""WebSocket handler utilities."""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable
from typing import Any

from ..exceptions import RequestValidationError


def build_websocket_request(scope: dict[str, Any]) -> dict[str, Any]:
    """
    Build a request-like dict from WebSocket scope for parameter injection.

    This function converts the WebSocket scope (similar to ASGI scope) into
    a request dict format that the HTTP parameter injector expects.

    The scope contains:
    - type: "websocket"
    - path: URL path
    - query_string: raw query string as bytes
    - headers: dict of header name -> value
    - path_params: dict of path parameter name -> value
    - cookies: dict of cookie name -> value
    - client: tuple of (host, port)

    Returns a dict compatible with the HTTP injector:
    - params: path parameters
    - query: parsed query string parameters
    - headers: request headers (lowercased keys)
    - cookies: request cookies
    - body: empty bytes (WebSocket has no request body at connect time)
    """
    # Use pre-coerced query_params from Rust scope (type coercion already done)
    # Rust builds query_params with properly typed values (int, uuid, etc.)
    query_map = scope.get("query_params", {})

    return {
        "params": scope.get("path_params", {}),
        "query": query_map,
        "headers": scope.get("headers", {}),
        "cookies": scope.get("cookies", {}),
        "body": b"",  # WebSocket has no body at connect time
        "path": scope.get("path", "/"),
        "method": "WEBSOCKET",
    }


# The name of each request source in the detail of a refused WebSocket upgrade, as Rust names it.
_SOURCE_LABELS = {"query": "Query parameter", "path": "Path parameter", "header": "Header", "cookie": "Cookie"}


def invalid_request_detail(error: BaseException) -> str | None:
    """Give the detail of a request value that a parameter extractor refused, or None.

    Rust refuses a bad scalar value before the handler runs, and the upgrade
    gets 400. An extractor refuses other values, such as an item of a
    ``list[int]``, with ``RequestValidationError``. Before accept, Bolt then
    refuses the upgrade with 400 too, with a detail of the same form:
    ``Query parameter 'ids': ...``. For another error, give None.

    An error item can come from a handler, so each key is optional. An item
    with no "msg" uses its "type", then "Invalid value".
    """
    if not isinstance(error, RequestValidationError):
        return None
    details = []
    for item in error.errors():
        if not isinstance(item, dict):
            details.append(str(item))
            continue
        message = item.get("msg") or item.get("type") or "Invalid value"
        loc = item.get("loc")
        if not isinstance(loc, (list, tuple)) or len(loc) < 2:
            details.append(str(message))
            continue
        source, *names = loc
        label = _SOURCE_LABELS.get(source, source) if isinstance(source, str) else source
        details.append(f"{label} '{'.'.join(str(name) for name in names)}': {message}")
    return "; ".join(details) or str(error)


def mark_websocket_handler(func: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a function as a WebSocket handler.

    A bound method cannot hold an attribute. The mark goes on its function, and
    a read of the attribute through the method finds it.
    """
    getattr(func, "__func__", func)._is_websocket_handler = True
    return func


def get_websocket_param_name(func: Callable[..., Any]) -> str | None:
    """
    Get the parameter name that expects a WebSocket object.

    Looks for parameter with WebSocket type annotation.
    """
    sig = inspect.signature(func)
    hints = {}

    with contextlib.suppress(Exception):
        hints = inspect.get_annotations(func)

    for param_name, param in sig.parameters.items():
        # Check annotation
        annotation = hints.get(param_name) or param.annotation
        if annotation is inspect.Parameter.empty:
            continue

        # Handle string annotations and actual types
        annotation_name = (
            annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))
        )

        if "WebSocket" in annotation_name:
            return param_name

    # Default to 'websocket' if no annotation found
    if "websocket" in sig.parameters:
        return "websocket"

    # Return first parameter as fallback
    params = list(sig.parameters.keys())
    return params[0] if params else None
