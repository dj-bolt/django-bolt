"""Response serialization utilities."""

from __future__ import annotations

import inspect
import mimetypes
import types
from collections.abc import (
    AsyncGenerator as AsyncGeneratorABC,
)
from collections.abc import (
    AsyncIterable as AsyncIterableABC,
)
from collections.abc import (
    AsyncIterator as AsyncIteratorABC,
)
from collections.abc import (
    Generator as GeneratorABC,
)
from collections.abc import (
    Iterable as IterableABC,
)
from collections.abc import (
    Iterator as IteratorABC,
)
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, Union, get_args, get_origin

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

# Integer body-kind tags sent to Rust (avoids String alloc per response in parse_response_wire).
# Must match the match arms in src/handler.rs parse_response_wire().
_BODY_BYTES: int = 0
_BODY_STREAM: int = 1
_BODY_FILE: int = 2

BodyKind = Literal[0, 1, 2]
ResponseWireV1 = tuple[int, ResponseMetaTuple | int, BodyKind, bytes | StreamingResponse | str]


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


def _wire_bytes(status: int, meta: ResponseMetaTuple | int, body: bytes) -> ResponseWireV1:
    return status, meta, _BODY_BYTES, body


def _wire_stream(status: int, meta: ResponseMetaTuple | int, stream: StreamingResponse) -> ResponseWireV1:
    return status, meta, _BODY_STREAM, stream


def _wire_file(status: int, meta: ResponseMetaTuple | int, path: str) -> ResponseWireV1:
    return status, meta, _BODY_FILE, path


# Pre-computed response metadata for common cases.
# Integer tags avoid per-response tuple parsing on the Rust side.
# Must match STATIC_META_* constants in src/response_meta.rs.
_RESPONSE_META_JSON: int = 0
_RESPONSE_META_PLAINTEXT: int = 1
_RESPONSE_META_OCTETSTREAM: int = 2
_RESPONSE_META_EMPTY: int = 3
_RESPONSE_META_HTML: int = 4
_TYPED_STREAM_MEDIA_TYPE = "application/x-ndjson"
_RAW_STREAM_CHUNK_TYPES = (bytes, bytearray, memoryview, str)
_STREAM_ANNOTATION_ORIGINS = {
    AsyncIterableABC,
    AsyncIteratorABC,
    AsyncGeneratorABC,
    IterableABC,
    IteratorABC,
    GeneratorABC,
}


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


_RESPONSE_INSTANCE_TYPES = (
    JSON,
    PlainText,
    HTML,
    Redirect,
    File,
    FileResponse,
    StreamingResponse,
    ResponseClass,
    DjangoHttpResponse,
)
_STATIC_RESPONSE_META = {
    "json": _RESPONSE_META_JSON,
    "plaintext": _RESPONSE_META_PLAINTEXT,
    "octetstream": _RESPONSE_META_OCTETSTREAM,
    "html": _RESPONSE_META_HTML,
}
_VALIDATION_NOOP_TYPES = {
    dict,
    list,
    str,
    int,
    float,
    bool,
    bytes,
    bytearray,
    type(None),
}


def _response_validation_error(exc: Exception) -> ResponseWireV1:
    err = f"Response validation error: {exc}"
    return _wire_bytes(
        500,
        _build_response_meta(
            "plaintext",
            {"content-type": "text/plain; charset=utf-8"},
            None,
        ),
        err.encode(),
    )


def _build_wire_meta(
    response_type: str,
    custom_headers: dict[str, str] | None,
    cookies: list[Cookie] | None,
) -> ResponseMetaTuple | int:
    if not custom_headers and not cookies:
        static_meta = _STATIC_RESPONSE_META.get(response_type)
        if static_meta is not None:
            return static_meta
    return _build_response_meta(response_type, custom_headers, cookies)


@cache
def _infer_wire_response_type(media_type: str) -> str:
    normalized = media_type.split(";", 1)[0].strip().lower()
    if normalized == "application/json" or normalized.endswith("+json"):
        return "json"
    if normalized == "text/plain":
        return "plaintext"
    if normalized == "text/html":
        return "html"
    return "octetstream"


@cache
def _is_json_media_type(media_type: str) -> bool:
    normalized = media_type.split(";", 1)[0].strip().lower()
    return normalized == "application/json" or normalized.endswith("+json")


def _render_response_body(content: Any, media_type: str) -> bytes:
    if _is_json_media_type(media_type):
        return _json.encode(content)
    if isinstance(content, memoryview):
        return content.tobytes()
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, str):
        return content.encode()
    return str(content).encode()


def _ensure_content_type_header(headers: dict[str, str] | None, media_type: str) -> dict[str, str]:
    normalized = headers.copy() if headers else {}
    if not any(k.lower() == "content-type" for k in normalized):
        normalized["content-type"] = media_type
    return normalized


def _response_validation_is_required(annotation: Any) -> bool:
    if annotation is None:
        return False
    return annotation not in _VALIDATION_NOOP_TYPES


def _compile_response_validator(meta: HandlerMetadata | dict[str, Any]) -> tuple[Any, Any]:
    response_type = meta.get("response_type")
    if not meta.get("validate_response", True) or not _response_validation_is_required(response_type):
        return None, None

    def validate_sync(value: Any) -> Any:
        return coerce_to_response_type(value, response_type, meta=meta)

    async def validate_async(value: Any) -> Any:
        return await coerce_to_response_type_async(value, response_type, meta=meta)

    return validate_sync, validate_async


def _compile_stream_item_validators(meta: HandlerMetadata | dict[str, Any]) -> tuple[Any, Any]:
    is_stream_annotation, item_type = meta.get("_stream_info", (False, None))
    if (
        not is_stream_annotation
        or item_type is None
        or item_type in _RAW_STREAM_CHUNK_TYPES
        or not meta.get("validate_response", True)
        or not _response_validation_is_required(item_type)
    ):
        return None, None

    def validate_sync(value: Any) -> Any:
        return coerce_to_response_type(value, item_type, meta=meta)

    async def validate_async(value: Any) -> Any:
        return await coerce_to_response_type_async(value, item_type, meta=meta)

    return validate_sync, validate_async


def _compile_queryset_serializers(meta: HandlerMetadata | dict[str, Any]) -> tuple[Any, Any]:
    field_names = meta.get("response_field_names")
    if not field_names:
        return None, None

    def serialize_sync(queryset: QuerySet) -> list[Any]:
        return list(queryset.values(*field_names))

    async def serialize_async(queryset: QuerySet) -> list[Any]:
        values_qs = queryset.values(*field_names)
        return await sync_to_async(list, thread_sensitive=True)(values_qs)

    return serialize_sync, serialize_async


def _compile_model_projector(meta: HandlerMetadata | dict[str, Any]) -> Any:
    """Compile a function that projects model instances to dicts using response field names.

    When validate_response=False and the handler returns a list of Django model
    instances (not dicts/Structs), we still need to extract the declared fields
    to produce JSON-serializable dicts. This is compiled once at registration time.

    Returns None if no response_field_names are available (no list[Struct] annotation).
    """
    field_names = meta.get("response_field_names")
    if not field_names:
        return None

    def project(items: list[Any]) -> list[dict[str, Any]]:
        return [{name: getattr(item, name, None) for name in field_names} for item in items]

    return project


def _extract_stream_item_type(annotation: Any) -> tuple[bool, Any | None]:
    """Return (is_stream_annotation, item_type) for streaming return annotations."""
    if annotation is None:
        return False, None

    if annotation in _STREAM_ANNOTATION_ORIGINS:
        return True, None

    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            is_stream, item_type = _extract_stream_item_type(arg)
            if is_stream:
                return True, item_type
        return False, None

    if origin in _STREAM_ANNOTATION_ORIGINS:
        args = get_args(annotation)
        return True, args[0] if args else None

    return False, None


def _is_stream_protocol_instance(value: Any) -> bool:
    """Check if a runtime value can be consumed as a stream."""
    if isinstance(value, (str, bytes, bytearray, memoryview, dict, list, tuple, set, frozenset, QuerySet)):
        return False
    return hasattr(value, "__aiter__") or hasattr(value, "__anext__") or hasattr(value, "__iter__") or hasattr(
        value, "__next__"
    )


def _serialize_stream_chunk_sync(
    chunk: Any, validator: Any | None,
) -> bytes | str | bytearray | memoryview:
    """Serialize one stream chunk for sync generators."""
    if isinstance(chunk, _RAW_STREAM_CHUNK_TYPES):
        return chunk

    chunk = _convert_serializers(chunk)
    if validator is not None:
        chunk = validator(chunk)
    return _json.encode(chunk) + b"\n"


async def _serialize_stream_chunk_async(
    chunk: Any, validator: Any | None,
) -> bytes | str | bytearray | memoryview:
    """Serialize one stream chunk for async generators."""
    if isinstance(chunk, _RAW_STREAM_CHUNK_TYPES):
        return chunk

    chunk = _convert_serializers(chunk)
    if validator is not None:
        chunk = await validator(chunk)
    return _json.encode(chunk) + b"\n"


def _wrap_sync_stream_chunks(content: Any, item_type: Any | None, meta: HandlerMetadata):
    validator = meta.get("_stream_item_validator")
    for chunk in content:
        yield _serialize_stream_chunk_sync(chunk, validator)


async def _wrap_async_stream_chunks(content: Any, item_type: Any | None, meta: HandlerMetadata):
    validator = meta.get("_stream_item_validator_async")
    async for chunk in content:
        yield await _serialize_stream_chunk_async(chunk, validator)


def _to_stream_wire(stream_response: StreamingResponse) -> ResponseWireV1:
    custom_headers: dict[str, str] = {"content-type": stream_response.media_type}
    if stream_response.headers:
        custom_headers.update(stream_response.headers)
    cookies = getattr(stream_response, "_cookies", None)
    resp_meta = _build_response_meta("streaming", custom_headers, cookies)
    return _wire_stream(stream_response.status_code, resp_meta, stream_response)


def _build_auto_streaming_response(
    result: Any,
    stream_info: tuple[bool, Any | None],
    meta: HandlerMetadata | dict[str, Any],
    status_code: int,
) -> StreamingResponse | None:
    """Auto-wrap generator/iterator return values into StreamingResponse."""
    is_stream_annotation, item_type = stream_info
    is_async_gen = inspect.isasyncgen(result)
    is_generator_result = is_async_gen or inspect.isgenerator(result)

    if not is_generator_result and not (is_stream_annotation and _is_stream_protocol_instance(result)):
        return None

    needs_json_chunk_encoding = is_stream_annotation and item_type is not None and item_type not in _RAW_STREAM_CHUNK_TYPES
    if needs_json_chunk_encoding:
        is_async_stream = is_async_gen or hasattr(result, "__aiter__")
        content = (
            _wrap_async_stream_chunks(result, item_type, meta)
            if is_async_stream
            else _wrap_sync_stream_chunks(result, item_type, meta)
        )
        return StreamingResponse(content, status_code=status_code, media_type=_TYPED_STREAM_MEDIA_TYPE)

    return StreamingResponse(result, status_code=status_code)


def compile_response_handlers(meta: HandlerMetadata | dict[str, Any]) -> None:
    validator_sync, validator_async = _compile_response_validator(meta)
    stream_validator_sync, stream_validator_async = _compile_stream_item_validators(meta)
    queryset_serializer_sync, queryset_serializer_async = _compile_queryset_serializers(meta)
    model_projector = _compile_model_projector(meta)

    meta["_response_validator"] = validator_sync
    meta["_response_validator_async"] = validator_async
    meta["_stream_item_validator"] = stream_validator_sync
    meta["_stream_item_validator_async"] = stream_validator_async
    meta["_queryset_serializer"] = queryset_serializer_sync
    meta["_queryset_serializer_async"] = queryset_serializer_async
    meta["_has_response_validation"] = validator_sync is not None or validator_async is not None

    default_status_code = meta["default_status_code"]
    stream_info = meta.get("_stream_info", (False, None))

    async def data_handler(result: Any, status_code: int | None = None) -> ResponseWireV1:
        current_status = default_status_code if status_code is None else status_code

        if result is None and current_status == 204:
            return _wire_bytes(204, _RESPONSE_META_EMPTY, b"")

        if isinstance(result, dict):
            return await _serialize_json_payload_async(result, current_status, validator_async)
        if isinstance(result, list):
            result = _convert_serializers(result)
            # When validate_response=False and list contains model instances (not dicts/Structs),
            # project them to dicts using pre-compiled field names from the response type annotation.
            if model_projector is not None and result and not isinstance(result[0], (dict, msgspec.Struct)):
                result = model_projector(result)
            return await _serialize_json_payload_async(result, current_status, validator_async)
        if isinstance(result, (bytes, bytearray)):
            return _wire_bytes(current_status, _RESPONSE_META_OCTETSTREAM, bytes(result))
        if isinstance(result, str):
            return _wire_bytes(current_status, _RESPONSE_META_PLAINTEXT, result.encode())

        auto_stream = _build_auto_streaming_response(result, stream_info, meta, current_status)
        if auto_stream is not None:
            return _to_stream_wire(auto_stream)

        original = result
        result = _convert_serializers(result)
        if result is not original:
            return await _serialize_json_payload_async(result, current_status, validator_async)

        if isinstance(result, QuerySet):
            if queryset_serializer_async is not None:
                result = await queryset_serializer_async(result)
            else:
                result = await sync_to_async(list, thread_sensitive=True)(result)
            return await _serialize_json_payload_async(result, current_status, validator_async)

        if isinstance(result, msgspec.Struct) or validator_async is not None:
            return await _serialize_json_payload_async(result, current_status, validator_async)

        raise TypeError(
            f"Handler returned unsupported type {type(result).__name__!r}. "
            f"Return dict, list, or a Bolt response type (JSON, PlainText, HTML, Redirect, etc.)"
        )

    def data_handler_sync(result: Any, status_code: int | None = None) -> ResponseWireV1:
        current_status = default_status_code if status_code is None else status_code

        if result is None and current_status == 204:
            return _wire_bytes(204, _RESPONSE_META_EMPTY, b"")

        if isinstance(result, dict):
            return _serialize_json_payload_sync(result, current_status, validator_sync)
        if isinstance(result, list):
            result = _convert_serializers(result)
            if model_projector is not None and result and not isinstance(result[0], (dict, msgspec.Struct)):
                result = model_projector(result)
            return _serialize_json_payload_sync(result, current_status, validator_sync)
        if isinstance(result, (bytes, bytearray)):
            return _wire_bytes(current_status, _RESPONSE_META_OCTETSTREAM, bytes(result))
        if isinstance(result, str):
            return _wire_bytes(current_status, _RESPONSE_META_PLAINTEXT, result.encode())

        auto_stream = _build_auto_streaming_response(result, stream_info, meta, current_status)
        if auto_stream is not None:
            return _to_stream_wire(auto_stream)

        original = result
        result = _convert_serializers(result)
        if result is not original:
            return _serialize_json_payload_sync(result, current_status, validator_sync)

        if isinstance(result, QuerySet):
            result = queryset_serializer_sync(result) if queryset_serializer_sync is not None else list(result)
            return _serialize_json_payload_sync(result, current_status, validator_sync)

        if isinstance(result, msgspec.Struct) or validator_sync is not None:
            return _serialize_json_payload_sync(result, current_status, validator_sync)

        raise TypeError(
            f"Handler returned unsupported type {type(result).__name__!r}. "
            f"Return dict, list, or a Bolt response type (JSON, PlainText, HTML, Redirect, etc.)"
        )

    async def response_type_handler(result: Any) -> ResponseWireV1:
        if isinstance(result, JSON):
            if validator_async is not None:
                try:
                    data_bytes = _json.encode(await validator_async(result.data))
                except Exception as exc:
                    return _response_validation_error(exc)
            else:
                data_bytes = result.to_bytes()
            cookies = getattr(result, "_cookies", None)
            return _wire_bytes(result.status_code, _build_wire_meta("json", result.headers, cookies), data_bytes)
        if isinstance(result, StreamingResponse):
            return _to_stream_wire(result)
        if isinstance(result, PlainText):
            return serialize_plaintext_response(result)
        if isinstance(result, HTML):
            return serialize_html_response(result)
        if isinstance(result, Redirect):
            return serialize_redirect_response(result)
        if isinstance(result, File):
            return serialize_file_response(result)
        if isinstance(result, FileResponse):
            return serialize_file_streaming_response(result)
        if isinstance(result, ResponseClass):
            if validator_async is not None:
                try:
                    body = _render_response_body(await validator_async(result.content), result.media_type)
                except Exception as exc:
                    return _response_validation_error(exc)
            else:
                body = _render_response_body(result.content, result.media_type)
            rt = _infer_wire_response_type(result.media_type)
            hdrs = _ensure_content_type_header(result.headers, result.media_type)
            cookies = getattr(result, "_cookies", None)
            return _wire_bytes(result.status_code, _build_wire_meta(rt, hdrs, cookies), body)
        if isinstance(result, DjangoHttpResponse):
            return serialize_django_response(result)
        raise TypeError(f"Unsupported response type {type(result).__name__!r}")

    def response_type_handler_sync(result: Any) -> ResponseWireV1:
        if isinstance(result, JSON):
            if validator_sync is not None:
                try:
                    data_bytes = _json.encode(validator_sync(result.data))
                except Exception as exc:
                    return _response_validation_error(exc)
            else:
                data_bytes = result.to_bytes()
            cookies = getattr(result, "_cookies", None)
            return _wire_bytes(result.status_code, _build_wire_meta("json", result.headers, cookies), data_bytes)
        if isinstance(result, StreamingResponse):
            return _to_stream_wire(result)
        if isinstance(result, PlainText):
            return serialize_plaintext_response(result)
        if isinstance(result, HTML):
            return serialize_html_response(result)
        if isinstance(result, Redirect):
            return serialize_redirect_response(result)
        if isinstance(result, File):
            return serialize_file_response(result)
        if isinstance(result, FileResponse):
            return serialize_file_streaming_response(result)
        if isinstance(result, ResponseClass):
            if validator_sync is not None:
                try:
                    body = _render_response_body(validator_sync(result.content), result.media_type)
                except Exception as exc:
                    return _response_validation_error(exc)
            else:
                body = _render_response_body(result.content, result.media_type)
            rt = _infer_wire_response_type(result.media_type)
            hdrs = _ensure_content_type_header(result.headers, result.media_type)
            cookies = getattr(result, "_cookies", None)
            return _wire_bytes(result.status_code, _build_wire_meta(rt, hdrs, cookies), body)
        if isinstance(result, DjangoHttpResponse):
            return serialize_django_response(result)
        raise TypeError(f"Unsupported response type {type(result).__name__!r}")

    meta["_default_response_handler"] = data_handler
    meta["_default_response_handler_sync"] = data_handler_sync
    meta["_response_type_handler"] = response_type_handler
    meta["_response_type_handler_sync"] = response_type_handler_sync


def is_response_instance(value: Any) -> bool:
    return isinstance(value, _RESPONSE_INSTANCE_TYPES)


async def serialize_response(result: Any, meta: HandlerMetadata | dict[str, Any]) -> ResponseWireV1:
    """Serialize handler result to HTTP response."""
    if is_response_instance(result):
        return await meta["_response_type_handler"](result)
    return await meta["_default_response_handler"](result)


def serialize_response_sync(result: Any, meta: HandlerMetadata | dict[str, Any]) -> ResponseWireV1:
    """Serialize handler result to HTTP response (sync version for sync handlers)."""
    if is_response_instance(result):
        return meta["_response_type_handler_sync"](result)
    return meta["_default_response_handler_sync"](result)


async def _serialize_json_payload_async(
    result: Any,
    status_code: int,
    validator: Any = None,
) -> ResponseWireV1:
    if validator is not None:
        try:
            result = await validator(result)
        except Exception as exc:
            return _response_validation_error(exc)

    return _wire_bytes(status_code, _RESPONSE_META_JSON, _json.encode(result))


def _serialize_json_payload_sync(
    result: Any,
    status_code: int,
    validator: Any = None,
) -> ResponseWireV1:
    if validator is not None:
        try:
            result = validator(result)
        except Exception as exc:
            return _response_validation_error(exc)

    return _wire_bytes(status_code, _RESPONSE_META_JSON, _json.encode(result))


def serialize_plaintext_response(result: PlainText) -> ResponseWireV1:
    """Serialize plain text response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_wire_meta("plaintext", result.headers, cookies)
    return _wire_bytes(result.status_code, resp_meta, result.to_bytes())


def serialize_html_response(result: HTML) -> ResponseWireV1:
    """Serialize HTML response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_wire_meta("html", result.headers, cookies)
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
    resp_meta = _build_wire_meta("redirect", custom_headers, cookies)
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
        return _wire_bytes(result.status_code, _build_wire_meta("redirect", headers, None), b"")

    # Generic Django HttpResponse - extract content and headers
    headers = dict(result.items())
    content = result.content if isinstance(result.content, bytes) else result.content.encode()
    return _wire_bytes(result.status_code, _build_wire_meta("octetstream", headers, None), content)


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
    resp_meta = _build_wire_meta("file", custom_headers, cookies)
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
    resp_meta = _build_wire_meta("file", custom_headers, cookies)
    return _wire_file(result.status_code, resp_meta, result.path)


async def serialize_json_data(
    result: Any, meta: HandlerMetadata | dict[str, Any], *, status_code: int | None = None
) -> ResponseWireV1:
    """Serialize dict/list/other data as JSON."""
    status = status_code if status_code is not None else meta["default_status_code"]
    return _wire_bytes(status, _RESPONSE_META_JSON, _json.encode(result))


def serialize_json_data_sync(
    result: Any, meta: HandlerMetadata | dict[str, Any], *, status_code: int | None = None
) -> ResponseWireV1:
    """Serialize dict/list/other data as JSON (sync version)."""
    status = status_code if status_code is not None else meta["default_status_code"]
    return _wire_bytes(status, _RESPONSE_META_JSON, _json.encode(result))

