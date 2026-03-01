"""Response serialization utilities."""

from __future__ import annotations

import mimetypes
from typing import TYPE_CHECKING, Any, Literal

import msgspec
from asgiref.sync import sync_to_async
from django.db.models import QuerySet
from django.http import HttpResponse as DjangoHttpResponse
from django.http import HttpResponseRedirect as DjangoHttpResponseRedirect

from . import _json
from ._kwargs import coerce_to_response_type, coerce_to_response_type_async
from .cookies import Cookie
from .responses import HTML, JSON, File, FileResponse, PlainText, Redirect, StreamingResponse
from .responses import Response as ResponseClass

if TYPE_CHECKING:
    from .typing import HandlerMetadata

# Type aliases for response formats
# Raw cookie tuple: (name, value, path, max_age, expires, domain, secure, httponly, samesite)
CookieTuple = tuple[str, str, str, int | None, str | None, str | None, bool, bool, str | None]

# ResponseMeta tuple: (response_type, custom_content_type, custom_headers, cookies)
# This is the new format for Rust-side header building
ResponseMetaTuple = tuple[
    str,  # response_type: "json", "html", "plaintext", etc.
    str | None,  # custom_content_type: override content-type or None
    list[tuple[str, str]] | None,  # custom_headers: [(key, value), ...] or None
    list[CookieTuple] | None,  # cookies: list of raw cookie tuples or None
]

BodyKind = Literal["bytes", "stream", "file"]
ResponseWireV1 = tuple[int, ResponseMetaTuple, BodyKind, bytes | StreamingResponse | str]

_BODY_BYTES: BodyKind = "bytes"
_BODY_STREAM: BodyKind = "stream"
_BODY_FILE: BodyKind = "file"


def _build_response_meta(
    response_type: str,
    custom_headers: dict[str, str] | None,
    cookies: list[Cookie] | None,
) -> ResponseMetaTuple:
    """Build metadata tuple for Rust header construction.

    Args:
        response_type: Type of response ("json", "html", "plaintext", etc.)
        custom_headers: Custom headers dict or None
        cookies: List of Cookie objects or None

    Returns:
        Tuple of (response_type, custom_content_type, custom_headers, cookies)
        suitable for Rust-side header building
    """
    custom_ct: str | None = None
    headers_list: list[tuple[str, str]] | None = None

    if custom_headers:
        # Extract custom content-type if provided
        headers_list = []
        for k, v in custom_headers.items():
            if k.lower() == "content-type":
                custom_ct = v
            else:
                # Don't lowercase here - Rust will do it
                headers_list.append((k, v))
        if not headers_list:
            headers_list = None

    # Convert cookies to raw tuples
    cookies_data: list[CookieTuple] | None = None
    if cookies:
        cookies_data = [c.to_raw_tuple() for c in cookies]

    return (response_type, custom_ct, headers_list, cookies_data)


def _wire_bytes(status: int, meta: ResponseMetaTuple, body: bytes) -> ResponseWireV1:
    return int(status), meta, _BODY_BYTES, body


def _wire_stream(status: int, meta: ResponseMetaTuple, stream: StreamingResponse) -> ResponseWireV1:
    return int(status), meta, _BODY_STREAM, stream


def _wire_file(status: int, meta: ResponseMetaTuple, path: str) -> ResponseWireV1:
    return int(status), meta, _BODY_FILE, path


# Pre-computed response metadata tuples for common cases (module-level constants)
_RESPONSE_META_JSON = _build_response_meta("json", None, None)
_RESPONSE_META_PLAINTEXT = _build_response_meta("plaintext", None, None)
_RESPONSE_META_OCTETSTREAM = _build_response_meta("octetstream", None, None)
_RESPONSE_META_EMPTY = _build_response_meta("empty", None, None)


def _convert_serializers(result: Any) -> Any:
    """
    Convert Serializer instances to dicts using dump().

    This ensures write_only fields are excluded and computed_field values are included.
    Uses a unique marker (__is_bolt_serializer__) to identify Serializers, avoiding
    false positives from duck typing with random objects that happen to have dump().

    Args:
        result: The handler result to potentially convert

    Returns:
        Converted result (dict/list if Serializer, original otherwise)
    """
    # Check for Serializer instance using unique marker (not duck typing)
    # __is_bolt_serializer__ is defined on the Serializer base class
    if getattr(result.__class__, "__is_bolt_serializer__", False) and hasattr(result, "dump"):
        return result.dump()

    # Handle list of Serializers
    if isinstance(result, list) and len(result) > 0:
        first = result[0]
        if getattr(first.__class__, "__is_bolt_serializer__", False) and hasattr(first, "dump"):
            return [item.dump() for item in result]

    return result


def _resolve_response_type(status_code: int, meta: HandlerMetadata) -> tuple[Any, HandlerMetadata]:
    """Resolve the response type for a status code in multi-response mode.

    Returns a pre-built meta dict with the resolved ``response_type``.
    """
    resolved_metas = meta["_resolved_metas"]
    if status_code in resolved_metas:
        resolved_meta = resolved_metas[status_code]
    elif ... in resolved_metas:
        resolved_meta = resolved_metas[...]
    else:
        response_map = meta["response_map"]
        raise TypeError(
            f"Status {status_code} has no response schema. "
            f"Defined: {sorted(c for c in response_map if c is not ...)}"
        )

    return resolved_meta["response_type"], resolved_meta


def _dispatch_non_json_type(
    result: Any, response_type: Any | None, meta: HandlerMetadata, status_code: int
) -> ResponseWireV1 | None:
    """Shared type dispatch for non-dict/list response types.

    Returns ResponseWireV1 for handled types, or None if type needs async handling.
    """
    # Handle different response types (ordered by frequency for performance)
    if isinstance(result, JSON):
        return None  # JSON needs async/sync-specific handling
    if isinstance(result, StreamingResponse):
        custom_headers: dict[str, str] = {"content-type": result.media_type}
        if result.headers:
            custom_headers.update(result.headers)
        cookies = getattr(result, "_cookies", None)
        resp_meta = _build_response_meta("streaming", custom_headers, cookies)
        return _wire_stream(result.status_code, resp_meta, result)
    if isinstance(result, PlainText):
        return serialize_plaintext_response(result)
    if isinstance(result, HTML):
        return serialize_html_response(result)
    if isinstance(result, (bytes, bytearray)):
        return _wire_bytes(status_code, _RESPONSE_META_OCTETSTREAM, bytes(result))
    if isinstance(result, str):
        return _wire_bytes(status_code, _RESPONSE_META_PLAINTEXT, result.encode())
    if isinstance(result, Redirect):
        return serialize_redirect_response(result)
    if isinstance(result, File):
        return serialize_file_response(result)
    if isinstance(result, FileResponse):
        return serialize_file_streaming_response(result)
    if isinstance(result, ResponseClass):
        return None  # ResponseClass needs async/sync-specific handling
    if isinstance(result, msgspec.Struct):
        return None  # Struct needs async/sync-specific JSON handling
    if isinstance(result, QuerySet):
        return None  # QuerySet needs async/sync-specific handling
    if isinstance(result, DjangoHttpResponse):
        return serialize_django_response(result)
    return None  # Signal unhandled type


async def serialize_response(result: Any, meta: HandlerMetadata) -> ResponseWireV1:
    """Serialize handler result to HTTP response."""
    # Direct access -- keys guaranteed at registration time
    status_code = meta["default_status_code"]
    response_type = meta["response_type"]

    # Handle 204 No Content
    if result is None and status_code == 204:
        return _wire_bytes(204, _RESPONSE_META_EMPTY, b"")

    # Fast path: dict/list are the most common response types (90%+ of handlers)
    if isinstance(result, dict):
        return await serialize_json_data(result, response_type, meta)
    if isinstance(result, list):
        result = _convert_serializers(result)
        return await serialize_json_data(result, response_type, meta)

    # Convert Serializer instances to dicts (handles write_only, computed_field)
    original = result
    result = _convert_serializers(result)

    # If _convert_serializers changed the value, it IS dict/list -- skip isinstance re-check
    if result is not original:
        return await serialize_json_data(result, response_type, meta)

    # Try shared dispatch first (handles most non-JSON types)
    shared_result = _dispatch_non_json_type(result, response_type, meta, status_code)
    if shared_result is not None:
        return shared_result

    # Async-specific handling for types that need await
    if isinstance(result, JSON):
        return await serialize_json_response(result, response_type, meta)
    if isinstance(result, ResponseClass):
        return await serialize_generic_response(result, response_type, meta)
    if isinstance(result, msgspec.Struct):
        return await serialize_json_data(result, response_type, meta)
    if isinstance(result, QuerySet):
        result_list = await sync_to_async(list, thread_sensitive=True)(result)
        return await serialize_json_data(result_list, response_type, meta)

    raise TypeError(
        f"Handler returned unsupported type {type(result).__name__!r}. "
        f"Return dict, list, or a Bolt response type (JSON, PlainText, HTML, Redirect, etc.)"
    )


def serialize_response_sync(result: Any, meta: HandlerMetadata) -> ResponseWireV1:
    """Serialize handler result to HTTP response (sync version for sync handlers)."""
    # Direct access -- keys guaranteed at registration time
    status_code = meta["default_status_code"]
    response_type = meta["response_type"]

    # Handle 204 No Content
    if result is None and status_code == 204:
        return _wire_bytes(204, _RESPONSE_META_EMPTY, b"")

    # Fast path: dict/list are the most common response types (90%+ of handlers)
    if isinstance(result, dict):
        return serialize_json_data_sync(result, response_type, meta)
    if isinstance(result, list):
        result = _convert_serializers(result)
        return serialize_json_data_sync(result, response_type, meta)

    # Convert Serializer instances
    original = result
    result = _convert_serializers(result)

    # If _convert_serializers changed the value, skip isinstance re-check
    if result is not original:
        return serialize_json_data_sync(result, response_type, meta)

    # Try shared dispatch first (handles most non-JSON types)
    shared_result = _dispatch_non_json_type(result, response_type, meta, status_code)
    if shared_result is not None:
        return shared_result

    # Sync-specific handling
    if isinstance(result, JSON):
        if response_type is not None:
            try:
                validated = coerce_to_response_type(result.data, response_type, meta=meta)
                data_bytes = _json.encode(validated)
            except Exception as e:
                err = f"Response validation error: {e}"
                return _wire_bytes(
                    500,
                    _build_response_meta(
                        "plaintext",
                        {"content-type": "text/plain; charset=utf-8"},
                        None,
                    ),
                    err.encode(),
                )
        else:
            data_bytes = result.to_bytes()
        cookies = getattr(result, "_cookies", None)
        resp_meta = _build_response_meta("json", result.headers, cookies)
        return _wire_bytes(result.status_code, resp_meta, data_bytes)
    elif isinstance(result, ResponseClass):
        if response_type is not None:
            try:
                validated = coerce_to_response_type(result.content, response_type, meta=meta)
                data_bytes = _json.encode(validated) if result.media_type == "application/json" else result.to_bytes()
            except Exception as e:
                err = f"Response validation error: {e}"
                return _wire_bytes(
                    500,
                    _build_response_meta(
                        "plaintext",
                        {"content-type": "text/plain; charset=utf-8"},
                        None,
                    ),
                    err.encode(),
                )
        else:
            data_bytes = result.to_bytes()

        response_type = "json" if result.media_type == "application/json" else "octetstream"
        headers = result.headers.copy() if result.headers else {}
        has_custom_content_type = any(k.lower() == "content-type" for k in headers)
        if not has_custom_content_type:
            headers["content-type"] = result.media_type

        cookies = getattr(result, "_cookies", None)
        resp_meta = _build_response_meta(response_type, headers, cookies)
        return _wire_bytes(result.status_code, resp_meta, data_bytes)
    elif isinstance(result, msgspec.Struct):
        return serialize_json_data_sync(result, response_type, meta)
    elif isinstance(result, QuerySet):
        return serialize_json_data_sync(list(result), response_type, meta)

    raise TypeError(
        f"Handler returned unsupported type {type(result).__name__!r}. "
        f"Return dict, list, or a Bolt response type (JSON, PlainText, HTML, Redirect, etc.)"
    )


async def serialize_generic_response(
    result: ResponseClass, response_tp: Any | None, meta: HandlerMetadata | None = None
) -> ResponseWireV1:
    """Serialize generic Response object with custom headers.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    if response_tp is not None:
        try:
            validated = await coerce_to_response_type_async(result.content, response_tp, meta=meta)
            data_bytes = _json.encode(validated) if result.media_type == "application/json" else result.to_bytes()
        except Exception as e:
            err = f"Response validation error: {e}"
            return _wire_bytes(
                500,
                _build_response_meta(
                    "plaintext",
                    {"content-type": "text/plain; charset=utf-8"},
                    None,
                ),
                err.encode(),
            )
    else:
        data_bytes = result.to_bytes()

    # Determine response type based on media_type
    response_type = "json" if result.media_type == "application/json" else "octetstream"

    # Build headers dict with media_type as content-type if not already provided
    headers = result.headers.copy() if result.headers else {}
    has_custom_content_type = any(k.lower() == "content-type" for k in headers)
    if not has_custom_content_type:
        headers["content-type"] = result.media_type

    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_response_meta(response_type, headers, cookies)
    return _wire_bytes(result.status_code, resp_meta, data_bytes)


async def serialize_json_response(
    result: JSON, response_tp: Any | None, meta: HandlerMetadata | None = None
) -> ResponseWireV1:
    """Serialize JSON response object.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    if response_tp is not None:
        try:
            validated = await coerce_to_response_type_async(result.data, response_tp, meta=meta)
            data_bytes = _json.encode(validated)
        except Exception as e:
            err = f"Response validation error: {e}"
            return _wire_bytes(
                500,
                _build_response_meta(
                    "plaintext",
                    {"content-type": "text/plain; charset=utf-8"},
                    None,
                ),
                err.encode(),
            )
    else:
        data_bytes = result.to_bytes()

    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_response_meta("json", result.headers, cookies)
    return _wire_bytes(result.status_code, resp_meta, data_bytes)


def serialize_plaintext_response(result: PlainText) -> ResponseWireV1:
    """Serialize plain text response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_response_meta("plaintext", result.headers, cookies)
    return _wire_bytes(result.status_code, resp_meta, result.to_bytes())


def serialize_html_response(result: HTML) -> ResponseWireV1:
    """Serialize HTML response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_response_meta("html", result.headers, cookies)
    return _wire_bytes(result.status_code, resp_meta, result.to_bytes())


def serialize_redirect_response(result: Redirect) -> ResponseWireV1:
    """Serialize redirect response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    # Build custom headers with location
    custom_headers: dict[str, str] = {"location": result.url}
    if result.headers:
        custom_headers.update(result.headers)

    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_response_meta("redirect", custom_headers, cookies)
    return _wire_bytes(result.status_code, resp_meta, b"")


def serialize_django_response(result: DjangoHttpResponse) -> ResponseWireV1:
    """Serialize Django HttpResponse types (e.g., from @login_required decorator).

    Only called in fallback path - no overhead for normal Bolt responses.
    """
    # Handle redirects specially (HttpResponseRedirect, HttpResponsePermanentRedirect)
    if isinstance(result, DjangoHttpResponseRedirect):
        headers: dict[str, str] = {"location": result.url}
        # Copy other headers from Django response
        for key, value in result.items():
            if key.lower() != "location":
                headers[key] = value
        return _wire_bytes(result.status_code, _build_response_meta("redirect", headers, None), b"")

    # Generic Django HttpResponse - extract content and headers
    headers = dict(result.items())
    content = result.content if isinstance(result.content, bytes) else result.content.encode()
    return _wire_bytes(result.status_code, _build_response_meta("octetstream", headers, None), content)


def serialize_file_response(result: File) -> ResponseWireV1:
    """Serialize file response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    data = result.read_bytes()
    ctype = result.media_type or mimetypes.guess_type(result.path)[0] or "application/octet-stream"

    # Build custom headers
    custom_headers: dict[str, str] = {"content-type": ctype}
    if result.filename:
        custom_headers["content-disposition"] = f'attachment; filename="{result.filename}"'
    if result.headers:
        custom_headers.update(result.headers)

    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_response_meta("file", custom_headers, cookies)
    return _wire_bytes(result.status_code, resp_meta, data)


def serialize_file_streaming_response(result: FileResponse) -> ResponseWireV1:
    """Serialize file streaming response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    ctype = result.media_type or mimetypes.guess_type(result.path)[0] or "application/octet-stream"

    # Build custom headers
    custom_headers: dict[str, str] = {
        "content-type": ctype,
    }
    if result.filename:
        custom_headers["content-disposition"] = f'attachment; filename="{result.filename}"'
    if result.headers:
        custom_headers.update(result.headers)

    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_response_meta("file", custom_headers, cookies)
    return _wire_file(result.status_code, resp_meta, result.path)


async def serialize_json_data(
    result: Any, response_tp: Any | None, meta: HandlerMetadata, *, status_code: int | None = None
) -> ResponseWireV1:
    """Serialize dict/list/other data as JSON.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    if response_tp is not None:
        try:
            validated = await coerce_to_response_type_async(result, response_tp, meta=meta)
            data = _json.encode(validated)
        except Exception as e:
            err = f"Response validation error: {e}"
            return _wire_bytes(
                500,
                _build_response_meta(
                    "plaintext",
                    {"content-type": "text/plain; charset=utf-8"},
                    None,
                ),
                err.encode(),
            )
    else:
        data = _json.encode(result)

    status = status_code if status_code is not None else meta["default_status_code"]
    return _wire_bytes(status, _RESPONSE_META_JSON, data)


def serialize_json_data_sync(
    result: Any, response_tp: Any | None, meta: HandlerMetadata, *, status_code: int | None = None
) -> ResponseWireV1:
    """Serialize dict/list/other data as JSON (sync version for sync handlers).

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    if response_tp is not None:
        try:
            validated = coerce_to_response_type(result, response_tp, meta=meta)
            data = _json.encode(validated)
        except Exception as e:
            err = f"Response validation error: {e}"
            return _wire_bytes(
                500,
                _build_response_meta(
                    "plaintext",
                    {"content-type": "text/plain; charset=utf-8"},
                    None,
                ),
                err.encode(),
            )
    else:
        data = _json.encode(result)

    status = status_code if status_code is not None else meta["default_status_code"]
    return _wire_bytes(status, _RESPONSE_META_JSON, data)
