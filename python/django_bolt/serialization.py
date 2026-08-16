"""Response serialization utilities."""

from __future__ import annotations

import asyncio
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
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Union, get_args, get_origin

import msgspec
from django.core.exceptions import FieldError
from django.db.models import QuerySet
from django.http import HttpResponse as DjangoHttpResponse
from django.http import HttpResponseRedirect as DjangoHttpResponseRedirect

from . import _json
from ._kwargs import coerce_to_response_type, coerce_to_response_type_async
from .concurrency import run_in_orm_executor
from .cookies import Cookie
from .exceptions import ResponseValidationError, UnloadedRelationError
from .responses import (
    HTML,
    JSON,
    EventSourceResponse,
    File,
    FileResponse,
    PlainText,
    Redirect,
    ServerSentEvent,
    StreamingResponse,
    format_sse_event,
)
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

# Bare-bytes wire fast path: _sync_executor variants (api.py) may return just the
# encoded JSON body (bytes) instead of a ResponseWireV1 tuple when the response
# is (route default status, JSON meta). Rust's sync dispatch rebuilds the
# envelope from RouteMetadata.default_status_code — see src/handler.rs.


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


def _raise_response_validation_error(exc: Exception) -> NoReturn:
    """Convert a low-level validation failure into ResponseValidationError.

    Handler returned data that doesn't match its declared response_model.
    Raising ResponseValidationError lets the central exception handler
    (`error_handlers.response_validation_error_handler`) emit a structured 500
    JSON response, log the bug, and apply debug/prod policy uniformly.
    """
    if isinstance(exc, msgspec.ValidationError):
        raise ResponseValidationError([exc]) from exc
    raise ResponseValidationError([{"loc": ["response"], "msg": str(exc), "type": "validation_error"}]) from exc


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


def _strip_optional(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _response_serializer_info(response_type: Any) -> tuple[Any, bool]:
    """Return (serializer_class, many) for Serializer or list[Serializer]."""
    rt = _strip_optional(response_type)

    if _is_bolt_serializer(rt):
        return rt, False

    if get_origin(rt) is list:
        args = get_args(rt)
        if args:
            item = _strip_optional(args[0])
            if _is_bolt_serializer(item):
                return item, True

    return None, False


def _annotation_may_need_relation_load(annotation: Any) -> bool:
    annotation = _strip_optional(annotation)
    return get_origin(annotation) in {list, tuple, set, frozenset}


def _dump_dict(serializer_cls: Any, value: dict, validate_dicts: bool) -> Any:
    """Validate already-serialized dicts only when response validation is on."""
    if not validate_dicts:
        return value
    return msgspec.convert(value, serializer_cls).dump()


def _is_serializer_instance(value: Any) -> bool:
    return getattr(value.__class__, "__is_bolt_serializer__", False)


def _dump_one_sync(serializer_cls: Any, value: Any, validate_dicts: bool) -> Any:
    if value is None:
        return value
    if _is_serializer_instance(value):
        return value.dump()
    if isinstance(value, dict):
        return _dump_dict(serializer_cls, value, validate_dicts)
    return serializer_cls.from_model(value).dump()


async def _dump_one_async(serializer_cls: Any, value: Any, sync_safe: bool, validate_dicts: bool) -> Any:
    if value is None:
        return value
    if _is_serializer_instance(value):
        return value.dump()
    if isinstance(value, dict):
        return _dump_dict(serializer_cls, value, validate_dicts)
    if sync_safe:
        return serializer_cls.from_model(value).dump()
    try:
        return serializer_cls.from_model(value).dump()
    except UnloadedRelationError:
        pass
    return (await serializer_cls.afrom_model(value)).dump()


def _dump_many_sync(serializer_cls: Any, items: Any, validate_dicts: bool) -> list[Any]:
    items = items if isinstance(items, list) else list(items)
    if not items:
        return items
    first = items[0]
    if isinstance(first, dict):
        return [_dump_dict(serializer_cls, item, validate_dicts) for item in items]
    if _is_serializer_instance(first):
        return [item.dump() for item in items]
    instances = [serializer_cls.from_model(item) for item in items]
    return serializer_cls.dump_many(instances)


async def _dump_many_async(serializer_cls: Any, items: Any, sync_safe: bool, validate_dicts: bool) -> list[Any]:
    items = items if isinstance(items, list) else list(items)
    if not items:
        return items
    first = items[0]
    if isinstance(first, dict):
        return [_dump_dict(serializer_cls, item, validate_dicts) for item in items]
    if _is_serializer_instance(first):
        return [item.dump() for item in items]
    if sync_safe:
        instances = [serializer_cls.from_model(item) for item in items]
    else:
        try:
            instances = [serializer_cls.from_model(item) for item in items]
        except UnloadedRelationError:
            instances = list(await asyncio.gather(*(serializer_cls.afrom_model(item) for item in items)))
    return serializer_cls.dump_many(instances)


def _serializer_from_model_sync_safe(serializer_cls: Any) -> bool:
    """True when async relation loading is unnecessary."""
    serializer_cls._ensure_from_model_ready()
    type_hints = getattr(serializer_cls, "__cached_type_hints__", {})
    return not any(
        spec.nested is not None
        or spec.source is not None
        or _annotation_may_need_relation_load(type_hints.get(spec.field_name))
        for spec in serializer_cls.__model_field_specs__
    )


def _serializer_values_path_eligible(serializer_cls: Any) -> bool:
    """True when a QuerySet response can be evaluated via .values(*fields).

    The values() fast path skips model instantiation (`from_db`/`__init__`,
    ~14% of ORM GIL time measured) AND per-instance `from_model` field
    extraction (~13%) by feeding column dicts straight to the existing
    dict-validation path. Requires a "plain" serializer: dump == plain field
    dict (no computed/write_only/nested/excluded fields), no renames (dict
    keys must match wire names for msgspec.convert), no source mappings, and
    no relation-typed fields.
    """
    serializer_cls._ensure_from_model_ready()
    if not getattr(serializer_cls, "__dump_fast_path__", False):
        return False
    if getattr(serializer_cls, "__has_rename__", False):
        return False
    return _serializer_from_model_sync_safe(serializer_cls)


def _compile_serializer_values_evaluators(serializer_cls: Any) -> tuple[Any, Any]:
    """Compile QuerySet evaluators using .values(*fields) with a per-model
    runtime fallback: a serializer field that is not a concrete column raises
    FieldError on first evaluation — that model is then permanently routed to
    the model-instance path (from_model) in this process."""
    field_names = tuple(serializer_cls.__struct_fields__)
    # queryset.model -> True (values() works) / False (fall back to instances).
    # Benign races under the GIL: both writers store the same verdict.
    model_ok: dict[type, bool] = {}

    def evaluate_sync(queryset: QuerySet) -> list[Any]:
        queryset = serializer_cls._prepare_queryset(queryset)  # Config.annotations & co.
        ok = model_ok.get(queryset.model)
        if ok is False:
            # Project here, on the ORM thread, so model properties may still
            # query (an N+1, never SynchronousOnlyOperation on the loop).
            return _dump_many_sync(serializer_cls, list(queryset), False)
        try:
            items = list(queryset.values(*field_names))
        except FieldError:
            # Serializer field isn't a concrete column on this model.
            model_ok[queryset.model] = False
            return _dump_many_sync(serializer_cls, list(queryset), False)
        model_ok[queryset.model] = True
        return items

    async def evaluate_async(queryset: QuerySet) -> list[Any]:
        return await run_in_orm_executor(evaluate_sync, queryset)

    return evaluate_sync, evaluate_async


def _compile_serializer_response_handlers(serializer_cls: Any, many: bool, validate_dicts: bool) -> tuple[Any, Any]:
    """Compile normalizers that render model/queryset output via the serializer."""
    sync_safe = _serializer_from_model_sync_safe(serializer_cls)

    if many:

        def normalize_sync(value: Any) -> Any:
            if not isinstance(value, (list, tuple)):
                return value
            return _dump_many_sync(serializer_cls, value, validate_dicts)

        async def normalize_async(value: Any) -> Any:
            if not isinstance(value, (list, tuple)):
                return value
            return await _dump_many_async(serializer_cls, value, sync_safe, validate_dicts)

        return normalize_sync, normalize_async

    def normalize_sync(value: Any) -> Any:
        return _dump_one_sync(serializer_cls, value, validate_dicts)

    async def normalize_async(value: Any) -> Any:
        return await _dump_one_async(serializer_cls, value, sync_safe, validate_dicts)

    return normalize_sync, normalize_async


def _compile_response_validator(meta: HandlerMetadata | dict[str, Any]) -> tuple[Any, Any]:
    response_type = meta.get("response_type")
    if not _response_validation_is_required(response_type):
        return None, None

    serializer_cls, many = _response_serializer_info(response_type)
    if serializer_cls is not None:
        return _compile_serializer_response_handlers(
            serializer_cls, many, validate_dicts=meta.get("validate_response", True)
        )

    if not meta.get("validate_response", True):
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
        # Bounded ORM pool: parallel (unlike sync_to_async's thread_sensitive
        # single thread) but capped so DB connection count stays small.
        return await run_in_orm_executor(list, values_qs)

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
    return (
        hasattr(value, "__aiter__")
        or hasattr(value, "__anext__")
        or hasattr(value, "__iter__")
        or hasattr(value, "__next__")
    )


def _serialize_stream_chunk_sync(
    chunk: Any,
    validator: Any | None,
) -> bytes | str | bytearray | memoryview:
    """Serialize one stream chunk for sync generators."""
    if isinstance(chunk, _RAW_STREAM_CHUNK_TYPES):
        return chunk

    chunk = _convert_serializers(chunk)
    if validator is not None:
        chunk = validator(chunk)
    return _json.encode(chunk) + b"\n"


async def _serialize_stream_chunk_async(
    chunk: Any,
    validator: Any | None,
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


# ═══════════════════════════════════════════════════════════════════════════
# SSE (Server-Sent Events) auto-framing
# ═══════════════════════════════════════════════════════════════════════════


def _serialize_sse_chunk(chunk: Any) -> bytes:
    """Serialize a single yielded item into SSE wire-format bytes."""
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        return bytes(chunk) if not isinstance(chunk, bytes) else chunk
    if isinstance(chunk, str):
        return chunk.encode("utf-8")
    if isinstance(chunk, ServerSentEvent):
        if chunk.raw_data is not None:
            return format_sse_event(
                data_str=chunk.raw_data, event=chunk.event, id=chunk.id, retry=chunk.retry, comment=chunk.comment
            )
        return format_sse_event(
            data_bytes=_json.encode(chunk.data) if chunk.data is not None else None,
            event=chunk.event,
            id=chunk.id,
            retry=chunk.retry,
            comment=chunk.comment,
        )
    # Plain objects (dict, list, msgspec.Struct, etc.)
    return format_sse_event(data_bytes=_json.encode(chunk))


def _wrap_sync_sse_chunks(content: Any):
    for chunk in content:
        yield _serialize_sse_chunk(chunk)


async def _wrap_async_sse_chunks(content: Any):
    async for chunk in content:
        yield _serialize_sse_chunk(chunk)


def _to_stream_wire(stream_response: StreamingResponse) -> ResponseWireV1:
    # Auto-wrap EventSourceResponse generator with SSE framing.
    # Mutate in place so ping_interval and other attributes are preserved for Rust.
    if isinstance(stream_response, EventSourceResponse):
        if stream_response.is_async_generator:
            stream_response.content = _wrap_async_sse_chunks(stream_response.content)
        else:
            stream_response.content = _wrap_sync_sse_chunks(stream_response.content)

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
    response_class: type | None = None,
) -> StreamingResponse | None:
    """Auto-wrap generator/iterator return values into StreamingResponse."""
    is_stream_annotation, item_type = stream_info
    is_async_gen = inspect.isasyncgen(result)
    is_generator_result = is_async_gen or inspect.isgenerator(result)

    if not is_generator_result and not (is_stream_annotation and _is_stream_protocol_instance(result)):
        return None

    # When response_class=EventSourceResponse, wrap as EventSourceResponse
    # SSE framing is applied later in _to_stream_wire
    if response_class is not None and issubclass(response_class, EventSourceResponse):
        return EventSourceResponse(result, status_code=status_code)

    needs_json_chunk_encoding = (
        is_stream_annotation and item_type is not None and item_type not in _RAW_STREAM_CHUNK_TYPES
    )
    if needs_json_chunk_encoding:
        is_async_stream = is_async_gen or hasattr(result, "__aiter__")
        content = (
            _wrap_async_stream_chunks(result, item_type, meta)
            if is_async_stream
            else _wrap_sync_stream_chunks(result, item_type, meta)
        )
        return StreamingResponse(content, status_code=status_code, media_type=_TYPED_STREAM_MEDIA_TYPE)

    return StreamingResponse(result, status_code=status_code)


def _is_bolt_serializer(t: Any) -> bool:
    """True if t opts into Bolt Serializer behaviour via __is_bolt_serializer__."""
    return getattr(t, "__is_bolt_serializer__", False)


def _is_plain_struct(t: Any) -> bool:
    """msgspec.Struct subclass, but NOT a Bolt Serializer."""
    try:
        return isinstance(t, type) and issubclass(t, msgspec.Struct) and not _is_bolt_serializer(t)
    except (TypeError, AttributeError):
        return False


def _union_of_plain_structs(t: Any) -> tuple[type, ...] | None:
    """If t is a Union of plain msgspec.Structs (optional None allowed), return the struct tuple."""
    origin = get_origin(t)
    if origin not in (Union, types.UnionType):
        return None
    args = [a for a in get_args(t) if a is not type(None)]
    if not args or not all(_is_plain_struct(a) for a in args):
        return None
    return tuple(args)


def _compile_inline_response_validator(
    meta: HandlerMetadata | dict[str, Any],
) -> tuple[str, Any] | None:
    """Pre-classify the response annotation for the inline-encoding fast path.

    Returns a descriptor consumed by the dispatcher in ``api.py``:
      - ``("struct", types_tuple)`` — runtime ``isinstance`` against ``types_tuple``;
        on match, encode directly via ``msgspec.json.Encoder``.
      - ``("list", annotation)`` — when the handler returns a Python list,
        ``msgspec.convert(result, annotation)`` then encode.
      - ``None`` — no inline path; the dispatcher falls back to
        ``serialize_response[_sync]``.

    The dispatcher always delegates non-matching runtime values (Response
    objects, QuerySets, generators, ``None``, dicts, Serializers, ...) to
    ``serialize_response`` so the slow path handles every edge case uniformly.

    Bails out at registration time for setups that need the slow path
    end-to-end:
      - ``validate_response=False``
      - ``response_class`` set (SSE/FileResponse/etc. need slow-path wrapping)
      - ``default_status_code == 204`` (None-body shortcut lives in the slow path)
      - ``is_multi_response`` (multi-status routes have their own dispatcher)
    """
    if not meta.get("validate_response", True):
        return None
    if meta.get("response_class") is not None:
        return None
    if meta.get("default_status_code") == 204:
        return None
    if meta.get("is_multi_response"):
        return None
    response_type = meta.get("response_type")
    if response_type is None or response_type in _VALIDATION_NOOP_TYPES:
        return None

    if _is_plain_struct(response_type):
        return ("struct", (response_type,))

    union_structs = _union_of_plain_structs(response_type)
    if union_structs is not None:
        return ("struct", union_structs)

    if get_origin(response_type) is list:
        args = get_args(response_type)
        if args:
            item_type = args[0]
            if _is_plain_struct(item_type) or _union_of_plain_structs(item_type) is not None:
                return ("list", response_type)

    return None


def compile_response_handlers(meta: HandlerMetadata | dict[str, Any]) -> None:
    validator_sync, validator_async = _compile_response_validator(meta)
    stream_validator_sync, stream_validator_async = _compile_stream_item_validators(meta)
    queryset_serializer_sync, queryset_serializer_async = _compile_queryset_serializers(meta)

    # Bolt Serializer list responses: evaluate QuerySets via .values(*fields)
    # when the serializer is plain — no model instantiation, no from_model
    # walk. The resulting dicts flow through the existing dict-validation path
    # so output is identical to the model-instance path.
    if queryset_serializer_sync is None:
        _values_serializer_cls, _values_many = _response_serializer_info(meta.get("response_type"))
        if (
            _values_serializer_cls is not None
            and _values_many
            and _serializer_values_path_eligible(_values_serializer_cls)
        ):
            queryset_serializer_sync, queryset_serializer_async = _compile_serializer_values_evaluators(
                _values_serializer_cls
            )
    model_projector = _compile_model_projector(meta)
    inline_validator = _compile_inline_response_validator(meta)

    meta["_response_validator"] = validator_sync
    meta["_response_validator_async"] = validator_async
    meta["_stream_item_validator"] = stream_validator_sync
    meta["_stream_item_validator_async"] = stream_validator_async
    meta["_queryset_serializer"] = queryset_serializer_sync
    meta["_queryset_serializer_async"] = queryset_serializer_async
    meta["_inline_response_validator"] = inline_validator
    meta["_has_response_validation"] = validator_sync is not None or validator_async is not None

    default_status_code = meta["default_status_code"]
    stream_info = meta.get("_stream_info", (False, None))
    response_class = meta.get("response_class")

    _serializer_cls, _serializer_many = _response_serializer_info(meta.get("response_type"))
    is_serializer_response = _serializer_cls is not None
    # list[Serializer] routes returning a QuerySet: pre-load what the serializer
    # reads (select_related/prefetch_related/annotate from its fields), then
    # from_model + dump. Async routes iterate with the async ORM on the loop.
    if _serializer_many:

        def evaluate_serializer_queryset(queryset: QuerySet) -> list[Any]:
            return _serializer_cls.dump_many(_serializer_cls.from_models(queryset))

        async def evaluate_serializer_queryset_async(queryset: QuerySet) -> list[Any]:
            return _serializer_cls.dump_many(await _serializer_cls.afrom_models(queryset))

    else:
        evaluate_serializer_queryset = evaluate_serializer_queryset_async = None

    def prepare_list(result: list[Any]) -> Any:
        if is_serializer_response:
            return result
        result = _convert_serializers(result)
        if model_projector is not None and result and not isinstance(result[0], (dict, msgspec.Struct)):
            return model_projector(result)
        return result

    def convert_serializer_result(result: Any) -> tuple[Any, bool]:
        if is_serializer_response:
            return result, False
        converted = _convert_serializers(result)
        return converted, converted is not result

    async def data_handler(result: Any, status_code: int | None = None) -> ResponseWireV1:
        current_status = default_status_code if status_code is None else status_code

        if result is None and current_status == 204:
            return _wire_bytes(204, _RESPONSE_META_EMPTY, b"")

        if isinstance(result, dict):
            return await _serialize_json_payload_async(result, current_status, validator_async)
        if isinstance(result, list):
            return await _serialize_json_payload_async(prepare_list(result), current_status, validator_async)
        if isinstance(result, (bytes, bytearray)):
            return _wire_bytes(current_status, _RESPONSE_META_OCTETSTREAM, bytes(result))
        if isinstance(result, str):
            return _wire_bytes(current_status, _RESPONSE_META_PLAINTEXT, result.encode())

        auto_stream = _build_auto_streaming_response(result, stream_info, meta, current_status, response_class)
        if auto_stream is not None:
            return _to_stream_wire(auto_stream)

        result, was_converted = convert_serializer_result(result)
        if was_converted:
            return await _serialize_json_payload_async(result, current_status, validator_async)

        if isinstance(result, QuerySet):
            if queryset_serializer_async is not None:
                result = await queryset_serializer_async(result)
            elif evaluate_serializer_queryset_async is not None:
                # Already dumped dicts — nothing left for the validator to do.
                result = await evaluate_serializer_queryset_async(result)
                return await _serialize_json_payload_async(result, current_status, None)
            else:
                # Bounded ORM pool — parallel, but capped connection count.
                result = await run_in_orm_executor(list, result)
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
            return _serialize_json_payload_sync(prepare_list(result), current_status, validator_sync)
        if isinstance(result, (bytes, bytearray)):
            return _wire_bytes(current_status, _RESPONSE_META_OCTETSTREAM, bytes(result))
        if isinstance(result, str):
            return _wire_bytes(current_status, _RESPONSE_META_PLAINTEXT, result.encode())

        auto_stream = _build_auto_streaming_response(result, stream_info, meta, current_status, response_class)
        if auto_stream is not None:
            return _to_stream_wire(auto_stream)

        result, was_converted = convert_serializer_result(result)
        if was_converted:
            return _serialize_json_payload_sync(result, current_status, validator_sync)

        if isinstance(result, QuerySet):
            if queryset_serializer_sync is not None:
                result = queryset_serializer_sync(result)
            elif evaluate_serializer_queryset is not None:
                return _serialize_json_payload_sync(evaluate_serializer_queryset(result), current_status, None)
            else:
                result = list(result)
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
                    validated = await validator_async(result.data)
                except ResponseValidationError:
                    raise
                except Exception as exc:
                    _raise_response_validation_error(exc)
                data_bytes = _json.encode(validated)
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
                    validated = await validator_async(result.content)
                except ResponseValidationError:
                    raise
                except Exception as exc:
                    _raise_response_validation_error(exc)
                body = _render_response_body(validated, result.media_type)
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
                    validated = validator_sync(result.data)
                except ResponseValidationError:
                    raise
                except Exception as exc:
                    _raise_response_validation_error(exc)
                data_bytes = _json.encode(validated)
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
                    validated = validator_sync(result.content)
                except ResponseValidationError:
                    raise
                except Exception as exc:
                    _raise_response_validation_error(exc)
                body = _render_response_body(validated, result.media_type)
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
        except ResponseValidationError:
            raise
        except Exception as exc:
            _raise_response_validation_error(exc)
    return _wire_bytes(status_code, _RESPONSE_META_JSON, _json.encode(result))


def _serialize_json_payload_sync(
    result: Any,
    status_code: int,
    validator: Any = None,
) -> ResponseWireV1:
    if validator is not None:
        try:
            result = validator(result)
        except ResponseValidationError:
            raise
        except Exception as exc:
            _raise_response_validation_error(exc)
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
