"""
Parameter binding and extraction with pre-compiled extractors.

This module provides high-performance parameter extraction using pre-compiled
extractor functions that avoid runtime type checking.
"""

from __future__ import annotations

import datetime
import enum
import inspect
import sys
from collections.abc import Callable
from functools import partial
from typing import Annotated, Any, Literal, NamedTuple, get_args, get_origin

import msgspec
from django.core.exceptions import FieldDoesNotExist
from django.db.models import ForeignObjectRel, Model, OneToOneRel, QuerySet, prefetch_related_objects
from django.db.models.query import ModelIterable

from .._core import parse_text_items
from .._field_marker import _FieldMarker
from .._serializer_before import RAW_VALIDATED, before_plan, run_before_validators
from .._serializer_input import ignore_read_only_input, read_only_input_extractor
from ..concurrency import run_in_orm_executor
from ..datastructures import UploadFile
from ..exceptions import HTTPException, RequestValidationError, parse_msgspec_decode_error
from ..typing import (
    FieldDefinition,
    is_msgspec_struct,
    is_optional,
    is_upload_file_type,
    resolve_type_alias,
    unwrap_optional,
)

__all__ = [
    "create_extractor_for_field",
    "create_path_extractor",
    "create_query_extractor",
    "create_header_extractor",
    "create_cookie_extractor",
    "create_form_extractor",
    "create_file_extractor",
    "create_body_extractor",
    "compile_response_coercer",
    "compile_struct_relation_loader",
    "compile_struct_row_readers",
    "compile_struct_row_steps",
    "get_msgspec_decoder",
    "StructRelationLoader",
]


# Cache for msgspec decoders (performance optimization)
_DECODER_CACHE: dict[Any, msgspec.json.Decoder] = {}


def get_msgspec_decoder(type_: Any) -> msgspec.json.Decoder:
    """Get or create a cached msgspec decoder for a type."""
    if type_ not in _DECODER_CACHE:
        _DECODER_CACHE[type_] = msgspec.json.Decoder(type_)
    return _DECODER_CACHE[type_]


_SEQUENCE_ORIGINS = (list, set, frozenset, tuple)
# The msgspec.Meta fields that constrain a value. The other fields only document it.
_META_CONSTRAINTS = ("gt", "ge", "lt", "le", "multiple_of", "pattern", "min_length", "max_length", "tz")
# These sources give one value. A query key can give more than one.
_SINGLE_VALUE_SOURCES = ("path", "header", "cookie")
# This map gives the `loc` source of a struct parameter error for each source name in messages.
# A scalar parameter from the same source has the same `loc` source.
_STRUCT_ERROR_SOURCES = {"query parameter": "query", "cookie": "cookie", "form field": "body"}
# The Rust type hints of the item types that Rust reads from text: the TYPE_*
# values of middleware/compiler.py. The middleware package imports this module,
# so the values are copied. A list of such items converts with the parser of a
# single value, so both accept the same text.
_TEXT_ITEM_HINTS = {int: 1, float: 2, datetime.datetime: 6, datetime.date: 8, datetime.time: 9}


def _text_item_hint(field_type: Any) -> int | None:
    """Give the Rust type hint of the items of a sequence type, or None.

    The type is ``list[T]``, ``set[T]``, ``frozenset[T]``, ``tuple[T]`` or
    ``tuple[T, ...]``, also behind ``Optional`` or ``Annotated``. ``T`` is
    ``int``, ``float``, ``date``, ``time`` or ``datetime``, also behind
    ``Annotated`` layers and type aliases. msgspec then gets the converted items and checks the
    ``msgspec.Meta`` constraints.
    """
    inner = unwrap_optional(resolve_type_alias(field_type))
    if get_origin(inner) is Annotated:
        inner = unwrap_optional(resolve_type_alias(get_args(inner)[0]))
    if get_origin(inner) not in _SEQUENCE_ORIGINS:
        return None
    items = [arg for arg in get_args(inner) if arg is not Ellipsis]
    if len(items) != 1:
        return None
    item = resolve_type_alias(items[0])
    # An alias can hold another Annotated layer, as in Annotated[X, ...] with type X = Annotated[int, ...].
    while get_origin(item) is Annotated:
        item = resolve_type_alias(get_args(item)[0])
    return _TEXT_ITEM_HINTS.get(item)


class _TextItems(NamedTuple):
    """How Rust converts the text items of a sequence type, found at registration."""

    hint: int
    # The type of the sequence when msgspec has nothing to check, else None.
    plain: type | None
    min_length: int
    max_length: int


# The msgspec type info of each sequence whose items Rust can convert, and its Python type.
_TEXT_ITEM_SEQUENCES = {
    msgspec.inspect.ListType: list,
    msgspec.inspect.SetType: set,
    msgspec.inspect.FrozenSetType: frozenset,
    msgspec.inspect.VarTupleType: tuple,
}


def _bare_type_info(node: msgspec.inspect.Type) -> msgspec.inspect.Type:
    """Give the msgspec type info under ``Metadata`` and ``Optional``."""
    while True:
        if isinstance(node, msgspec.inspect.Metadata):
            node = node.type
        elif isinstance(node, msgspec.inspect.UnionType):
            arms = [arm for arm in node.types if not isinstance(arm, msgspec.inspect.NoneType)]
            if len(arms) != 1:
                return node
            node = arms[0]
        else:
            return node


def _text_items_plan(field_type: Any) -> _TextItems | None:
    """Give how Rust converts the text items of a sequence type, or None.

    A ``list``, ``set``, ``frozenset`` or ``tuple[T, ...]`` with no length
    bound and no item constraint is plain: Rust builds the items, and
    msgspec has nothing to check. A ``tuple[T]`` holds one item.
    """
    hint = _text_item_hint(field_type)
    if hint is None:
        return None
    node = _bare_type_info(msgspec.inspect.type_info(field_type))
    if isinstance(node, msgspec.inspect.TupleType):
        return _TextItems(hint, None, 1, 1)
    item = _bare_type_info(node.item_type)
    unbounded = node.min_length is None and node.max_length is None
    plain = unbounded and all(getattr(item, name) is None for name in item.__struct_fields__)
    return _TextItems(
        hint,
        _TEXT_ITEM_SEQUENCES[type(node)] if plain else None,
        node.min_length or 0,
        sys.maxsize if node.max_length is None else node.max_length,
    )


def _item_error(loc: str, key: str, error: ValueError, value: list[Any]) -> dict[str, Any]:
    """Give the error of the item that ``parse_text_items`` refused, as msgspec names an item."""
    index, message = error.args
    return {"type": "validation_error", "loc": (loc, key), "msg": f"{message} - at `$[{index}]`", "input": value}


def _item_fields_plan(fields: Any) -> tuple[tuple[str, str, int, int, int], ...]:
    """Give the key, the ``loc`` name, the type hint and the length bounds of each typed sequence field.

    ``fields`` gives the key, the ``loc`` name and the type of each field of a parameter model.
    """
    plan = []
    for key, name, field_type in fields:
        items = _text_items_plan(field_type)
        if items is not None:
            plan.append((key, name, items.hint, items.min_length, items.max_length))
    return tuple(plan)


def _convert_item_fields(
    values: dict[str, Any], fields: tuple[tuple[str, str, int, int, int], ...], loc: str
) -> dict[str, dict[str, Any]] | None:
    """Convert the text items of the typed sequence fields of a parameter model in ``values``.

    ``values`` is a dict of the extractor, not of the request. A field with a
    length out of its bounds keeps its texts, so msgspec gives the length
    error. Give the error of each field with a refused item, by its key, or
    None when no item is refused.
    """
    refused = None
    for key, name, hint, min_length, max_length in fields:
        value = values.get(key)
        if type(value) is list:
            try:
                values[key] = parse_text_items(value, hint, min_length, max_length)
            except ValueError as error:
                if refused is None:
                    refused = {}
                refused[key] = _item_error(loc, name, error, value)
    return refused


def _item_text_inputs(
    inputs: dict[str, Any],
    request_values: Any,
    fields: tuple[tuple[str, str, int, int, int], ...],
    files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Give a copy of ``inputs`` with the request text of each typed sequence field, for the "input" of an error.

    ``_convert_item_fields`` puts the converted items in place of the text.
    The request still holds the text, under the ``loc`` name of the field.
    A file part replaces the text parts of its name, and it is never
    converted. Thus a field in ``files`` keeps its value.
    Only the error path calls this function.
    """
    texts = dict(inputs)
    for key, name, *_ in fields:
        if files is not None and key in files:
            continue
        value = request_values.get(name)
        if value is not None:
            texts[key] = value if isinstance(value, list) else [value]
    return texts


def _holds_one_item(annotation: Any) -> bool:
    """Return True when msgspec accepts a sequence of exactly one item for this type."""
    node = msgspec.inspect.type_info(annotation)
    if isinstance(node, msgspec.inspect.TupleType):
        return len(node.item_types) == 1
    return (node.min_length or 0) <= 1 and (node.max_length is None or node.max_length >= 1)


def _refuse_many_item_default(loc: str, key: str, annotation: Any, default: Any) -> None:
    """Raise ``TypeError`` when a one-value sequence parameter has a default of two or more items.

    A path, a header or a cookie gives one value. Bolt keeps the last line of a
    repeated header and does not split a comma list. Thus a sequence from such
    a source holds one item, and the OpenAPI schema says ``maxItems: 1``. A
    default of two items is not valid for that schema, so the route fails here.
    """
    if isinstance(default, _SEQUENCE_ORIGINS) and len(default) > 1 and _is_sequence_field(annotation):
        raise TypeError(
            f"The {loc} parameter {key!r} gets at most one item, but its default {default!r} "
            f"has {len(default)} items. Use a default of at most one item, or use Query() for a list of values."
        )


def _schema_default(field: msgspec.structs.FieldInfo) -> Any:
    """Return the default of a model field that the OpenAPI schema shows.

    The schema calls the ``default_factory`` of a serializer ``field()``. It
    calls no other factory that can give two items. A field with
    ``include_in_schema=False`` is not in the schema.
    """
    default = field.default
    if isinstance(default, _FieldMarker):
        config = default.config
        if not config.include_in_schema or not config.has_default():
            return msgspec.NODEFAULT
        return config.get_default()
    return default


def _param_converter(annotation: Any, loc: str, key: str, *, one_item: bool = False) -> Callable | None:
    """Build the conversion of one parameter value that Rust cannot do, or None.

    Rust converts a scalar value by its type. It does not build a sequence, it
    does not check ``msgspec.Meta`` constraints, and it does not know an
    ``Enum``, a ``Literal`` or a ``timedelta``. For such a parameter, one
    ``msgspec.convert`` builds the ``list``, ``set``, ``frozenset`` or ``tuple``
    from the values, checks the constraints, and gives the ``Enum`` member, the
    ``Literal`` value or the ``timedelta``. A bad value gives a 422 that
    names ``loc`` and ``key``. With ``one_item``, a source that gives one value
    (a path, a header or a cookie) gives a sequence of that one item. A type
    that cannot hold one item, such as ``tuple[int, str]``, then raises
    ``TypeError`` here, when the route registers. A form field (``loc`` is
    ``body``) that is not a sequence keeps the last value of a repeated key.
    """
    target = unwrap_optional(annotation)
    base = get_args(target)[0] if get_origin(target) is Annotated else target
    inner = unwrap_optional(base)
    # A bare `set` has no origin. msgspec reads it as set[Any].
    is_sequence = (get_origin(inner) or inner) in _SEQUENCE_ORIGINS
    bytes_items = _bytes_items(inner, is_sequence)
    if bytes_items is not None:
        return _bytes_param_converter(target, loc, key, items=bytes_items, wrap=is_sequence and one_item)
    constrained = get_origin(target) is Annotated and any(
        getattr(extra, name, None) is not None
        for extra in get_args(target)[1:]
        if isinstance(extra, msgspec.Meta)
        for name in _META_CONSTRAINTS
    )
    is_choice = get_origin(inner) is Literal or (isinstance(inner, type) and issubclass(inner, enum.Enum))
    # Rust has no conversion for a duration. msgspec reads the ISO 8601 form, such as PT5S.
    is_duration = inner is datetime.timedelta
    if not is_sequence and not constrained and not is_choice and not is_duration:
        return None
    wrap = is_sequence and one_item
    # Rust gives each value of a repeated form key as a list.
    take_last = loc == "body" and not is_sequence
    if wrap and loc in _SINGLE_VALUE_SOURCES and not _holds_one_item(target):
        raise TypeError(
            f"The {loc} parameter {key!r} gets one value, but its type {target!r} cannot hold one item. "
            "Use list[T], set[T], tuple[T] or tuple[T, ...]."
        )
    # msgspec caches the type info of a Struct, but not of a type such as list[int].
    # A one-field Struct made here keeps that lookup out of each request.
    holder = msgspec.defstruct("ParamValue", [("value", target)], array_like=True)

    def convert(value: Any) -> Any:
        if wrap and not isinstance(value, list):
            value = [value]
        elif take_last and type(value) is list:
            value = value[-1]
        try:
            return msgspec.convert((value,), holder, strict=False).value
        except msgspec.ValidationError as error:
            # Remove the position of the value in the holder from the error path.
            msg = str(error).replace(" - at `$[0]`", "").replace("`$[0]", "`$")
            raise RequestValidationError(
                errors=[{"type": "validation_error", "loc": (loc, key), "msg": msg, "input": value}]
            ) from None

    text_items = _text_items_plan(target) if is_sequence else None
    if text_items is None:
        return convert
    hint, plain, min_length, max_length = text_items

    if plain is not None:
        # Rust builds each item, and msgspec has nothing to check.
        def convert_plain(value: Any) -> Any:
            if wrap and not isinstance(value, list):
                value = [value]
            if type(value) is list:
                try:
                    items = parse_text_items(value, hint)
                except ValueError as error:
                    raise RequestValidationError(errors=[_item_error(loc, key, error, value)]) from None
                # The list itself comes back when an item is not text and no earlier text is refused.
                # msgspec then refuses it.
                if items is not value:
                    return items if plain is list else plain(items)
            return convert(value)

        return convert_plain

    def convert_items(value: Any) -> Any:
        if wrap and not isinstance(value, list):
            value = [value]
        items = value
        if type(value) is list:
            # A length out of bounds gives the list itself, so msgspec refuses it before it reads an item.
            try:
                items = parse_text_items(value, hint, min_length, max_length)
            except ValueError as error:
                raise RequestValidationError(errors=[_item_error(loc, key, error, value)]) from None
        try:
            return msgspec.convert((items,), holder, strict=False).value
        except msgspec.ValidationError as error:
            msg = str(error).replace(" - at `$[0]`", "").replace("`$[0]", "`$")
            raise RequestValidationError(
                errors=[{"type": "validation_error", "loc": (loc, key), "msg": msg, "input": value}]
            ) from None

    return convert_items


# A text parameter of one of these types gets the UTF-8 bytes of the text.
_BYTES_TYPES = (bytes, bytearray, memoryview)


def _bytes_items(inner: Any, is_sequence: bool) -> bool | frozenset[int] | None:
    """Say which text values of a type become ``bytes``, or None when the type holds no bytes type.

    False is a bytes type that is not a sequence. True is a sequence with one
    item type that is a bytes type, such as ``list[bytes]``. A frozenset holds
    the positions of the bytes items of a tuple such as ``tuple[bytes, int]``.
    """
    if not is_sequence:
        return False if inner in _BYTES_TYPES else None
    is_bytes = [
        (get_args(arg)[0] if get_origin(arg) is Annotated else arg) in _BYTES_TYPES
        for arg in get_args(inner)
        if arg is not Ellipsis
    ]
    if len(is_bytes) == 1:
        return True if is_bytes[0] else None
    return frozenset(position for position, found in enumerate(is_bytes) if found) or None


def _encode_items(values: list[Any], items: bool | frozenset[int]) -> list[Any]:
    """Encode the text items of a sequence that ``_bytes_items`` names."""
    if items is True:
        return [value.encode() if type(value) is str else value for value in values]
    return [
        value.encode() if position in items and type(value) is str else value for position, value in enumerate(values)
    ]


def _bytes_field_plan(struct_type: type) -> tuple[tuple[str, bool | frozenset[int]], ...]:
    """Return the encoded name and the ``_bytes_items`` result of each field of a model that holds bytes."""
    plan: list[tuple[str, bool | frozenset[int]]] = []
    for field in msgspec.structs.fields(struct_type):
        target = unwrap_optional(field.type)
        inner = unwrap_optional(get_args(target)[0] if get_origin(target) is Annotated else target)
        items = _bytes_items(inner, (get_origin(inner) or inner) in _SEQUENCE_ORIGINS)
        if items is not None:
            plan.append((getattr(field, "encode_name", field.name), items))
    return tuple(plan)


def _encode_bytes_fields(data: dict[str, Any], plan: tuple[tuple[str, bool | frozenset[int]], ...]) -> dict[str, Any]:
    """Return a copy of the values of a parameter model with the text of each bytes field as UTF-8 bytes.

    msgspec reads a str for a ``bytes`` type as base64. A parameter model gets
    text from a query, a form, a header or a cookie, as a plain parameter does.
    Thus the text is encoded before msgspec builds the model.
    """
    encoded = dict(data)
    for name, items in plan:
        value = encoded.get(name)
        if items is False:
            if type(value) is str:
                encoded[name] = value.encode()
        elif type(value) is list:
            encoded[name] = _encode_items(value, items)
    return encoded


def _bytes_param_converter(target: Any, loc: str, key: str, *, items: bool | frozenset[int], wrap: bool) -> Callable:
    """Build the conversion of a text parameter to ``bytes``, or to a sequence of ``bytes``.

    Rust gives the value of a path segment, a query key, a header, a cookie or
    a form field as text. The parameter gets the UTF-8 bytes of that text.
    msgspec reads a str for a ``bytes`` type as base64. Thus this conversion
    encodes the text first. Then msgspec builds the sequence or the
    ``bytearray`` and checks the ``msgspec.Meta`` constraints.

    Rust gives each value of a repeated form key as a list. A parameter that is
    not a sequence keeps the last value, as for a repeated query key.
    """
    if target is bytes:
        # Plain bytes: msgspec has nothing to build and nothing to check.
        def encode(value: Any) -> bytes:
            return (value[-1] if type(value) is list else value).encode()

        return encode

    if wrap and loc in _SINGLE_VALUE_SOURCES and not _holds_one_item(target):
        raise TypeError(
            f"The {loc} parameter {key!r} gets one value, but its type {target!r} cannot hold one item. "
            "Use list[T], set[T], tuple[T] or tuple[T, ...]."
        )
    holder = msgspec.defstruct("ParamValue", [("value", target)], array_like=True)
    is_sequence = items is not False
    # The other items of a tuple such as tuple[bytes, int] are text. msgspec converts them.
    strict = type(items) is bool

    def convert(value: Any) -> Any:
        if type(value) is list:
            encoded = _encode_items(value, items) if is_sequence else value[-1].encode()
        else:
            encoded = [value.encode()] if wrap else value.encode()
        try:
            return msgspec.convert((encoded,), holder, strict=strict).value
        except msgspec.ValidationError as error:
            # Remove the position of the value in the holder from the error path.
            msg = str(error).replace(" - at `$[0]`", "").replace("`$[0]", "`$")
            raise RequestValidationError(
                errors=[{"type": "validation_error", "loc": (loc, key), "msg": msg, "input": value}]
            ) from None

    return convert


def _converted_extractor(key: str, annotation: Any, default: Any, convert: Callable, missing: str) -> Callable:
    """Build an extractor that reads ``key`` and converts a present value with ``convert``.

    A missing value gives the default of the parameter, which is not converted,
    or a 422 with the ``missing`` detail when the parameter is required.
    """
    if default is not inspect.Parameter.empty or is_optional(annotation):
        default_value = None if default is inspect.Parameter.empty else default

        def extract_optional(source_map: dict[str, Any]) -> Any:
            if key in source_map:
                return convert(source_map[key])
            return default_value

        return extract_optional

    def extract_required(source_map: dict[str, Any]) -> Any:
        try:
            value = source_map[key]
        except KeyError:
            raise HTTPException(status_code=422, detail=missing) from None
        return convert(value)

    return extract_required


def create_path_extractor(name: str, annotation: Any, alias: str | None = None) -> Callable:
    """Create a pre-compiled extractor for path parameters.

    Note: Rust pre-converts values to typed Python objects (int, float, bool,
    str, uuid.UUID, decimal.Decimal, datetime, date, time) for both HTTP and WebSocket.
    """
    key = alias or name
    convert = _param_converter(annotation, "path", key, one_item=True)

    if convert is not None:

        def extract_converted(params_map: dict[str, Any]) -> Any:
            if key not in params_map:
                raise HTTPException(status_code=422, detail=f"Missing required path parameter: {key}")
            return convert(params_map[key])

        return extract_converted

    def extract(params_map: dict[str, Any]) -> Any:
        if key not in params_map:
            raise HTTPException(status_code=422, detail=f"Missing required path parameter: {key}")
        return params_map[key]

    return extract


def create_query_extractor(
    name: str, annotation: Any, default: Any, alias: str | None = None, other_keys: frozenset[str] = frozenset()
) -> Callable:
    """Create a pre-compiled extractor for query parameters.

    Supports both individual fields and Struct/Serializer types.
    When annotation is a msgspec.Struct or Serializer, extracts all struct
    fields from the query parameters and constructs the struct instance.

    Note: Rust pre-converts values to typed Python objects (int, float, bool,
    str, uuid.UUID, decimal.Decimal, datetime, date, time) for both HTTP and WebSocket.
    """
    # Check if annotation is a Struct/Serializer type
    unwrapped = unwrap_optional(annotation)
    if is_msgspec_struct(unwrapped):
        return _create_param_struct_extractor(unwrapped, default, "query parameter", other_keys)

    # Individual field extraction
    key = alias or name
    # HTTP routes get each value of a sequence key as a list from Rust. A
    # WebSocket scope gives the last value of a key, so it gives one item.
    convert = _param_converter(annotation, "query", key, one_item=True)
    if convert is not None:
        return _converted_extractor(key, annotation, default, convert, f"Missing required query parameter: {key}")
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract(query_map: dict[str, Any]) -> Any:
            if key in query_map:
                return query_map[key]
            return default_value
    else:

        def extract(query_map: dict[str, Any]) -> Any:
            if key not in query_map:
                raise HTTPException(status_code=422, detail=f"Missing required query parameter: {key}")
            return query_map[key]

    return extract


def create_header_extractor(name: str, annotation: Any, default: Any, alias: str | None = None) -> Callable:
    """Create a pre-compiled extractor for HTTP headers.

    Supports both individual fields and Struct/Serializer types.
    When annotation is a msgspec.Struct or Serializer, extracts all struct
    fields from the headers and constructs the struct instance.

    Note: The compiler puts the non-string header types into ``header_types``,
    keyed by header name. Rust converts and validates those values for HTTP
    and WebSocket, and a bad value gives a 422 (HTTP) or a rejected upgrade.
    The extractor thus reads typed objects (int, float, bool, uuid.UUID,
    decimal.Decimal, datetime, date, time) and does no conversion.
    """
    # Check if annotation is a Struct/Serializer type
    unwrapped = unwrap_optional(annotation)
    if is_msgspec_struct(unwrapped):
        return _create_header_struct_extractor(unwrapped, default)

    # Individual field extraction
    # Convert underscores to hyphens for HTTP header lookup
    # e.g., x_custom -> x-custom, content_type -> content-type
    key = (alias or name).lower().replace("_", "-")
    convert = _param_converter(annotation, "header", key, one_item=True)
    if convert is not None:
        return _converted_extractor(key, annotation, default, convert, f"Missing required header: {key}")
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract(headers_map: dict[str, Any]) -> Any:
            if key in headers_map:
                return headers_map[key]
            return default_value
    else:

        def extract(headers_map: dict[str, Any]) -> Any:
            # One dict lookup on the hit path; the try block costs nothing there.
            try:
                return headers_map[key]
            except KeyError:
                raise HTTPException(status_code=422, detail=f"Missing required header: {key}") from None

    return extract


def create_cookie_extractor(
    name: str, annotation: Any, default: Any, alias: str | None = None, other_keys: frozenset[str] = frozenset()
) -> Callable:
    """Create a pre-compiled extractor for cookies.

    Supports both individual fields and Struct/Serializer types.
    When annotation is a msgspec.Struct or Serializer, extracts all struct
    fields from the cookies and constructs the struct instance.

    Note: The compiler puts the non-string cookie types into ``cookie_types``,
    keyed by cookie name. Rust converts and validates those values for HTTP
    and WebSocket, and a bad value gives a 422 (HTTP) or a rejected upgrade.
    The extractor thus reads typed objects (int, float, bool, uuid.UUID,
    decimal.Decimal, datetime, date, time) and does no conversion.
    """
    # Check if annotation is a Struct/Serializer type
    unwrapped = unwrap_optional(annotation)
    if is_msgspec_struct(unwrapped):
        return _create_param_struct_extractor(unwrapped, default, "cookie", other_keys)

    # Individual field extraction
    key = alias or name
    convert = _param_converter(annotation, "cookie", key, one_item=True)
    if convert is not None:
        return _converted_extractor(key, annotation, default, convert, f"Missing required cookie: {key}")
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract(cookies_map: dict[str, Any]) -> Any:
            if key in cookies_map:
                return cookies_map[key]
            return default_value
    else:

        def extract(cookies_map: dict[str, Any]) -> Any:
            # One dict lookup on the hit path; the try block costs nothing there.
            try:
                return cookies_map[key]
            except KeyError:
                raise HTTPException(status_code=422, detail=f"Missing required cookie: {key}") from None

    return extract


def create_form_extractor(
    name: str, annotation: Any, default: Any, alias: str | None = None, other_keys: frozenset[str] = frozenset()
) -> Callable:
    """Create a pre-compiled extractor for form fields.

    Supports both individual fields and Struct/Serializer types.
    When annotation is a msgspec.Struct or Serializer, extracts all struct
    fields from the form data and constructs the struct instance.

    Note: Rust pre-converts values to typed Python objects (int, float, bool, str).
    Rust gives each value of a repeated key as a list. A field that is not a
    sequence keeps the last value, as a query parameter does. An ``Any`` field
    gets the list.
    """
    # Check if annotation is a Struct/Serializer type
    unwrapped = unwrap_optional(annotation)
    if is_msgspec_struct(unwrapped):
        return _create_param_struct_extractor(unwrapped, default, "form field", other_keys)

    # Individual field extraction
    key = alias or name
    convert = _param_converter(annotation, "body", key)
    if convert is not None:
        return _converted_extractor(key, annotation, default, convert, f"Missing required form field: {key}")
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract_all(form_map: dict[str, Any]) -> Any:
            return form_map.get(key, default_value)

        def extract(form_map: dict[str, Any]) -> Any:
            if key not in form_map:
                return default_value
            value = form_map[key]
            return value[-1] if type(value) is list else value
    else:

        def extract_all(form_map: dict[str, Any]) -> Any:
            if key not in form_map:
                raise HTTPException(status_code=422, detail=f"Missing required form field: {key}")
            return form_map[key]

        def extract(form_map: dict[str, Any]) -> Any:
            try:
                value = form_map[key]
            except KeyError:
                raise HTTPException(status_code=422, detail=f"Missing required form field: {key}") from None
            return value[-1] if type(value) is list else value

    return extract_all if unwrapped is Any else extract


def _upload_file_dec_hook(typ: type, obj: Any) -> Any:
    """dec_hook for msgspec.convert() that builds UploadFile from Rust file_info dicts.

    Non-dict inputs (e.g. a plain text form value sent under a file field)
    must raise msgspec.ValidationError so _collect_struct_errors can surface
    them as a structured 422 instead of crashing in from_file_info.
    """
    if isinstance(obj, typ):
        return obj
    if typ is UploadFile:
        if not isinstance(obj, dict):
            raise msgspec.ValidationError(f"Expected uploaded file, got {type(obj).__name__}")
        return UploadFile.from_file_info(obj)
    raise NotImplementedError(f"Unsupported type: {typ}")


def _collect_struct_errors(
    struct_type: type,
    data: dict[str, Any],
    param_type: str,
    source: str,
    has_files: bool = False,
    inputs: dict[str, Any] | None = None,
    refused: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Collect validation errors for every field of a msgspec struct from the provided input mapping.

    Validate each field of `struct_type` against `data` and return a list of error objects describing either per-field validation failures or missing required fields/files. The `loc` of each error names the source as a scalar parameter does ("query", "cookie" or "body"). When `has_files` is True, the function decodes upload files for validation.

    Parameters:
        struct_type (type): The msgspec struct/serializer type whose fields to validate.
        data (dict[str, Any]): The input mapping of encoded field names to values.
        param_type (str): The source name in a missing-field message ("query parameter", "cookie" or "form field").
        source (str): The source in `loc` ("query", "cookie" or "body").
        has_files (bool): Enable upload-file decoding hooks when True.
        inputs (dict[str, Any] | None): The values as the request gave them, for the "input" of an error. `data` holds the UTF-8 bytes of a bytes field. The default is `data`.
        refused (dict[str, dict[str, Any]] | None): The error of each field with an item that `parse_text_items` refused, by encoded name.

    Returns:
        list[dict[str, Any]]: A list of error dictionaries. Each dictionary contains:
            - "type": one of "validation_error", "missing_field", or "file_missing".
            - "loc": a tuple locating the error (e.g., ("query", "field") or ("body", "field")).
            - "msg": a human-readable message describing the issue.
            - "input": the offending input value or None for missing fields/files.
    """
    dec_hook = _upload_file_dec_hook if has_files else None
    if inputs is None:
        inputs = data
    errors: list[dict[str, Any]] = []
    for field in msgspec.structs.fields(struct_type):
        encoded_name = getattr(field, "encode_name", field.name)
        if refused and encoded_name in refused:
            errors.append(refused[encoded_name])
        elif encoded_name in data:
            try:
                msgspec.convert(data[encoded_name], type=field.type, strict=False, dec_hook=dec_hook)
            except (msgspec.ValidationError, NotImplementedError) as e:
                errors.append(
                    {
                        "type": "validation_error",
                        "loc": (source, encoded_name),
                        "msg": str(e),
                        "input": inputs[encoded_name],
                    }
                )
        elif field.required:
            is_file = is_upload_file_type(field.type)
            errors.append(
                {
                    "type": "file_missing" if is_file else "missing_field",
                    "loc": ("body", encoded_name) if is_file else (source, encoded_name),
                    "msg": "Missing required file" if is_file else f"Missing required {param_type}: {encoded_name}",
                    "input": None,
                }
            )
    return errors


def _is_sequence_field(field_type: Any) -> bool:
    """
    Determine whether a type annotation represents a sequence container (list, set, frozenset, or tuple).

    Unwraps Optional[...], Annotated[...] and a type alias before checking the underlying origin. A bare ``set`` has no origin, so the class itself counts.

    Parameters:
        field_type (Any): The type annotation to inspect.

    Returns:
        bool: `True` if the (unwrapped) annotation's origin is one of list, set, frozenset, or tuple; `False` otherwise.
    """
    inner = unwrap_optional(resolve_type_alias(field_type))
    if get_origin(inner) is Annotated:
        inner = unwrap_optional(resolve_type_alias(get_args(inner)[0]))
    return (get_origin(inner) or inner) in _SEQUENCE_ORIGINS


def _collect_sequence_field_names(struct_type: type) -> tuple[str, ...]:
    """
    Identify encoded (wire-side) field names for struct fields whose annotated type is a sequence.

    Used at extractor registration time so callers can precompute which incoming keys should be wrapped
    as single-element lists when the wire value is a scalar.

    Returns:
        tuple[str, ...]: Encoded field names (using `encode_name` when present, otherwise the field name)
        whose target type is a sequence (list/set/tuple/frozenset).
    """
    names: list[str] = []
    for f in msgspec.structs.fields(struct_type):
        if _is_sequence_field(f.type):
            names.append(getattr(f, "encode_name", f.name))
    return tuple(names)


def _or_struct_default(
    extract: Callable, default: Any, names: tuple[str, ...], needs_files: bool | None = None
) -> Callable:
    """Give an optional struct parameter its default when the request has none of its keys.

    ``names`` holds the keys that the struct reads. With one of them in the
    request, ``extract`` builds and validates the struct, as for a required
    parameter. A required parameter (no default) keeps ``extract`` as it is.
    ``needs_files`` is the ``needs_files_map`` flag of a query, cookie or form
    struct extractor, and None for a header struct extractor.
    """
    if default is inspect.Parameter.empty:
        return extract

    if needs_files:

        def extract_or_default(param_map: dict[str, Any], files_map: dict[str, Any] | None = None) -> Any:
            for name in names:
                if name in param_map or (files_map is not None and name in files_map):
                    return extract(param_map, files_map)
            return default

    else:

        def extract_or_default(param_map: dict[str, Any]) -> Any:
            for name in names:
                if name in param_map:
                    return extract(param_map)
            return default

    if needs_files is not None:
        extract_or_default.needs_files_map = needs_files  # type: ignore[attr-defined]
    return extract_or_default


def _unknown_field_errors(
    data: dict[str, Any], known: frozenset[str], param_type: str, source: str, files: Any = ()
) -> list[dict[str, Any]]:
    """Return one error for each key of ``data`` that is not a field of the struct.

    The input of an uploaded file (a key of ``files``) is not put in the error.
    """
    return [
        {
            "type": "unknown_field",
            "loc": (source, key),
            "msg": f"Unknown {param_type}: {key}",
            "input": None if key in files else value,
        }
        for key, value in data.items()
        if key not in known
    ]


@read_only_input_extractor
def _create_param_struct_extractor(
    struct_type: type, default: Any, param_type: str, other_keys: frozenset[str] = frozenset()
) -> Callable:
    """
    Create an extractor that builds and validates a msgspec Struct/Serializer from parameter data.

    The returned callable accepts parameter maps (and, when the struct has upload-file fields, an optional files map), converts the incoming values into an instance of `struct_type`, and performs validation. On validation failures the extractor raises RequestValidationError containing collected per-field errors. When the struct contains upload-file fields the extractor will also append created UploadFile instances to `files_map["_upload_files"]` for downstream cleanup.

    Behavioral notes:
    - Sequence-typed struct fields (e.g., list[T], tuple[T, ...], set[T]) accept single scalar wire values: scalars are wrapped into one-element lists before conversion.
    - The returned callable has an attribute `needs_files_map` set to True when upload-file fields are present (callable signature: (param_map, files_map | None) -> struct) and False otherwise (callable signature: (param_map) -> struct).

    Parameters:
        struct_type (type): msgspec struct/serializer type to produce.
        default (Any): The value of the parameter when the request has none of the keys of the struct. `inspect.Parameter.empty` makes the struct required.
        param_type (str): The source name in error messages ("query parameter", "form field" or "cookie"). It also selects the source in `loc`.
        other_keys (frozenset[str]): The keys that the other parameters of the handler read from the same source. A struct that forbids unknown fields does not get them. A key that no parameter declares gives a 422 with the type `unknown_field`.

    Returns:
        Callable: An extractor function that produces a validated instance of `struct_type` from incoming parameter data and raises RequestValidationError on validation errors.
    """
    # An unknown source fails here, when the route registers.
    source = _STRUCT_ERROR_SOURCES[param_type]
    # Check at registration time if any fields need file handling
    has_upload_file_fields = any(is_upload_file_type(f.type) for f in msgspec.structs.fields(struct_type))
    # Pre-compute sequence field names — empty tuple means zero runtime overhead.
    _seq_field_names = _collect_sequence_field_names(struct_type)
    # Empty for a model with no bytes field, so such a model has no cost for each request.
    _bytes_fields = _bytes_field_plan(struct_type)
    # The sequence fields whose text items Rust converts. Empty for most models.
    _item_fields = _item_fields_plan(
        (f.encode_name, f.encode_name, f.type) for f in msgspec.structs.fields(struct_type)
    )
    _item_keys = frozenset(key for key, *_ in _item_fields)
    # A struct that forbids unknown fields must not get the keys of other parameters.
    # For any other struct, the set is empty and the request path does not change.
    forbids_unknown = struct_type.__struct_config__.forbid_unknown_fields
    known = frozenset(f.encode_name for f in msgspec.structs.fields(struct_type))
    _other_keys = other_keys - known if forbids_unknown else frozenset()

    if has_upload_file_fields:
        _upload_field_names = [f.name for f in msgspec.structs.fields(struct_type) if is_upload_file_type(f.type)]

        def extract_with_files(param_map: dict[str, Any], files_map: dict[str, Any] | None = None) -> Any:
            """
            Builds and returns a msgspec struct by merging parameter values with uploaded-file data and converting them into the target struct type.

            Parameters:
                param_map (dict[str, Any]): Mapping of parameter names to their wire values (e.g., form/query/cookie fields).
                files_map (dict[str, Any] | None): Optional mapping of file field names to uploaded-file info; a list of created UploadFile instances will be appended to `files_map["_upload_files"]`.

            Returns:
                The converted instance of the pre-registered msgspec struct type.

            Raises:
                RequestValidationError: When validation errors are collected from the struct conversion (reported as structured errors).
                msgspec.ValidationError: Re-raised when conversion fails but no structured errors were collected.
            """
            files_map = files_map or {}
            merged = {**param_map}
            for k, v in files_map.items():
                if k != "_upload_files":
                    merged[k] = v
            for k in _other_keys:
                merged.pop(k, None)

            # Wrap scalar values for list[T]-typed fields so a single
            # form occurrence still produces a one-element list.
            # A request with no field of text items does no other work for them.
            has_items = False
            for name in _seq_field_names:
                value = merged.get(name)
                if value is not None:
                    if not isinstance(value, list):
                        merged[name] = [value]
                    if name in _item_keys:
                        has_items = True

            values = _encode_bytes_fields(merged, _bytes_fields) if _bytes_fields else merged
            if has_items:
                refused = _convert_item_fields(values, _item_fields, source)
                if refused is not None:
                    raise RequestValidationError(
                        errors=_collect_struct_errors(
                            struct_type,
                            values,
                            param_type,
                            source,
                            has_files=True,
                            inputs=_item_text_inputs(merged, param_map, _item_fields, files_map),
                            refused=refused,
                        )
                    )
            try:
                result = msgspec.convert(values, struct_type, strict=False, dec_hook=_upload_file_dec_hook)
            except msgspec.ValidationError as e:
                inputs = _item_text_inputs(merged, param_map, _item_fields, files_map) if has_items else merged
                errors = _collect_struct_errors(struct_type, values, param_type, source, has_files=True, inputs=inputs)
                if not errors and forbids_unknown:
                    errors = _unknown_field_errors(merged, known, param_type, source, files_map)
                if errors:
                    raise RequestValidationError(errors=errors) from e
                raise

            # Track UploadFile instances for auto-cleanup
            upload_files = files_map.setdefault("_upload_files", [])
            for name in _upload_field_names:
                value = getattr(result, name, None)
                if value is not None:
                    if isinstance(value, list):
                        upload_files.extend(value)
                    else:
                        upload_files.append(value)

            return result

        extract_with_files.needs_files_map = True  # type: ignore[attr-defined]
        names = tuple(f.encode_name for f in msgspec.structs.fields(struct_type))
        return _or_struct_default(extract_with_files, default, names, needs_files=True)
    else:
        # Extractor without file support (query params, cookies, non-file forms)
        def extract(param_map: dict[str, Any]) -> Any:
            """
            Convert a mapping of encoded parameter names to values into an instance of the target struct, wrapping scalar values into single-element lists for precomputed sequence fields.

            Parameters:
                param_map (dict[str, Any]): Mapping of encoded field names (as received from the request source) to their wire values.

            Returns:
                Any: An instance of the pre-bound `struct_type` produced by msgspec.convert.

            Raises:
                RequestValidationError: If msgspec conversion fails and detailed per-field errors are collected.
                msgspec.ValidationError: If msgspec conversion fails but no per-field errors were produced.
            """
            # Only allocate a copy when there's actual wrapping to do, or text items to convert.
            # A request with no field of text items does no other work for them.
            wrapped: dict[str, Any] | None = None
            has_items = False
            if _seq_field_names:
                for name in _seq_field_names:
                    value = param_map.get(name)
                    if value is None:
                        continue
                    is_items = name in _item_keys
                    if not isinstance(value, list):
                        value = [value]
                    elif not is_items:
                        continue
                    has_items = has_items or is_items
                    if wrapped is None:
                        # The request values keep the text of each item, for the "input" of an error.
                        request_map = param_map
                        wrapped = dict(param_map)
                    wrapped[name] = value
                param_map = wrapped if wrapped is not None else param_map

            if _other_keys:
                # Only allocate a copy when the request has a key of another parameter.
                for name in _other_keys:
                    if name in param_map:
                        param_map = {k: v for k, v in param_map.items() if k not in _other_keys}
                        break

            values = _encode_bytes_fields(param_map, _bytes_fields) if _bytes_fields else param_map
            if has_items:
                # A field with text items made `param_map` a copy above.
                refused = _convert_item_fields(values, _item_fields, source)
                if refused is not None:
                    inputs = _item_text_inputs(param_map, request_map, _item_fields)
                    raise RequestValidationError(
                        errors=_collect_struct_errors(
                            struct_type, values, param_type, source, inputs=inputs, refused=refused
                        )
                    )
            try:
                return msgspec.convert(values, struct_type, strict=False)
            except msgspec.ValidationError as e:
                inputs = _item_text_inputs(param_map, request_map, _item_fields) if has_items else param_map
                errors = _collect_struct_errors(struct_type, values, param_type, source, inputs=inputs)
                if not errors and forbids_unknown:
                    errors = _unknown_field_errors(param_map, known, param_type, source)
                if errors:
                    raise RequestValidationError(errors=errors) from e
                raise

        extract.needs_files_map = False  # type: ignore[attr-defined]
        names = tuple(f.encode_name for f in msgspec.structs.fields(struct_type))
        return _or_struct_default(extract, default, names, needs_files=False)


def _collect_header_struct_errors(
    header_fields: tuple[tuple[str, str, Any, bool], ...],
    data: dict[str, Any],
    inputs: dict[str, Any],
    strict: bool = True,
    refused: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return one error for each missing or invalid field of a header struct.

    ``header_fields`` holds the header name, the encoded name, the type and the
    required flag of each field. ``data`` maps encoded names to header values,
    with the UTF-8 bytes of a bytes field. ``inputs`` holds the values as the
    request gave them, for the ``input`` of an error.
    ``strict`` is the conversion mode of the extractor.
    ``refused`` holds the error of each field with an item that
    ``parse_text_items`` refused, by encoded name.
    The ``loc`` of each error is ``("header", header name)``, as for a scalar header.
    """
    errors: list[dict[str, Any]] = []
    for header_name, encoded_name, field_type, required in header_fields:
        if refused and encoded_name in refused:
            errors.append(refused[encoded_name])
        elif encoded_name in data:
            try:
                msgspec.convert(data[encoded_name], type=field_type, strict=strict)
            except msgspec.ValidationError as e:
                errors.append(
                    {
                        "type": "validation_error",
                        "loc": ("header", header_name),
                        "msg": str(e),
                        "input": inputs[encoded_name],
                    }
                )
        elif required:
            errors.append(
                {
                    "type": "missing_field",
                    "loc": ("header", header_name),
                    "msg": f"Missing required header: {header_name}",
                    "input": None,
                }
            )
    return errors


@read_only_input_extractor
def _create_header_struct_extractor(struct_type: type, default: Any) -> Callable:
    """Create an extractor that builds a Struct/Serializer from HTTP headers.

    Similar to _create_param_struct_extractor but converts field names from
    snake_case to kebab-case for HTTP header lookup. Supports msgspec field
    aliases (field(name=...)) and rename strategies.

    Note: Rust converts the typed struct fields before this runs (see
    ``create_header_extractor``), so msgspec gets typed values. A missing or
    invalid field gives a RequestValidationError with one error for each field.

    A header gives one value. A sequence field (``list``, ``set``, ``frozenset``
    or ``tuple``) gets a sequence of that one item, as a sequence header
    parameter does. Rust does not convert the item, so a struct with a sequence
    field converts with ``strict=False``, as the cookie struct does. An
    ``int``, ``float``, ``date``, ``time`` or ``datetime`` item first converts
    with ``parse_text_items``, as a single header value does.

    Args:
        struct_type: The msgspec.Struct or Serializer class
        default: The value of the parameter when the request has none of the
            headers of the struct. ``inspect.Parameter.empty`` makes the struct required.

    Returns:
        Extractor function that takes headers_map and returns struct instance
    """
    # Pre-compute header name mapping at registration time
    # Maps kebab-case header name → encoded field name (for msgspec alias support)
    header_to_encoded: list[tuple[str, str, bool]] = []
    header_fields: list[tuple[str, str, Any, bool]] = []
    for field in msgspec.structs.fields(struct_type):
        encoded_name = getattr(field, "encode_name", field.name)
        header_name = encoded_name.lower().replace("_", "-")
        header_to_encoded.append((header_name, encoded_name, _is_sequence_field(field.type)))
        header_fields.append((header_name, encoded_name, field.type, field.required))
    error_fields = tuple(header_fields)
    bytes_fields = _bytes_field_plan(struct_type)
    strict = not any(wrap for _, _, wrap in header_to_encoded)
    # The sequence fields whose text item Rust converts. Empty for most models.
    item_fields = _item_fields_plan(
        (encoded_name, header_name, field_type) for header_name, encoded_name, field_type, _ in header_fields
    )
    item_keys = frozenset(key for key, *_ in item_fields)
    header_plan = tuple(
        (header_name, encoded_name, wrap, encoded_name in item_keys)
        for header_name, encoded_name, wrap in header_to_encoded
    )

    def extract(headers_map: dict[str, str]) -> Any:
        converted = {}
        # A request with no field of text items does no other work for them.
        has_items = False
        for header_name, encoded_name, wrap, is_items in header_plan:
            value = headers_map.get(header_name)
            if value is not None:
                converted[encoded_name] = [value] if wrap else value
                has_items = has_items or is_items

        values = _encode_bytes_fields(converted, bytes_fields) if bytes_fields else converted
        if has_items:
            refused = _convert_item_fields(values, item_fields, "header")
            if refused is not None:
                inputs = _item_text_inputs(converted, headers_map, item_fields)
                raise RequestValidationError(
                    errors=_collect_header_struct_errors(error_fields, values, inputs, strict, refused)
                )
        try:
            return msgspec.convert(values, struct_type, strict=strict)
        except msgspec.ValidationError as e:
            inputs = _item_text_inputs(converted, headers_map, item_fields) if has_items else converted
            errors = _collect_header_struct_errors(error_fields, values, inputs, strict)
            if errors:
                raise RequestValidationError(errors=errors) from e
            raise

    return _or_struct_default(extract, default, tuple(header_name for header_name, _, _, _ in error_fields))


def create_file_extractor(
    name: str,
    annotation: Any,
    default: Any,
    alias: str | None = None,
) -> Callable:
    """
    Create a pre-compiled extractor for file uploads with UploadFile support.

    Supports both the new UploadFile type and legacy dict annotations for
    backward compatibility. A ``bytes`` parameter gets the content of the
    file, and ``list[bytes]`` gets the content of each file.

    Note: Rust does the file validation (max_size, min_size, allowed_types,
    max_files) at parse time, from the route metadata. This function only
    creates UploadFile objects from the pre-validated file data.

    Args:
        name: Parameter name
        annotation: Type annotation (UploadFile, list[UploadFile], bytes, list[bytes], dict, list[dict])
        default: Default value
        alias: Alternative field name

    Returns:
        Extractor function
    """
    key = alias or name
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    # Determine expected type
    unwrapped = unwrap_optional(annotation)
    origin = get_origin(unwrapped)
    expects_list = origin is list

    # Check if we should create UploadFile instances
    # Use is_upload_file_type helper which handles all cases including Optional
    expects_upload_file = is_upload_file_type(annotation)

    item_type = (get_args(unwrapped) or (Any,))[0] if expects_list else unwrapped
    if item_type is bytes:
        # The handler gets the content, not the file dict from Rust.
        default_value = None if default is inspect.Parameter.empty else default

        def read_content(file_info: dict[str, Any]) -> bytes:
            content = file_info["content"]
            if content is None:
                # Rust wrote a large file to disk. It deletes the file after the request.
                with open(file_info["temp_path"], "rb") as spooled:
                    return spooled.read()
            return content

        def extract_bytes(files_map: dict[str, Any]) -> Any:
            try:
                file_info = files_map[key]
            except KeyError:
                if optional:
                    return default_value
                raise RequestValidationError(
                    errors=[
                        {"type": "file_missing", "loc": ("body", key), "msg": "Missing required file", "input": None}
                    ]
                ) from None
            # Rust gives one dict for one file, and a list of dicts for more files.
            if expects_list:
                if isinstance(file_info, list):
                    return [read_content(item) for item in file_info]
                return [read_content(file_info)]
            # As for UploadFile: a parameter for one file gets the first file.
            return read_content(file_info[0] if isinstance(file_info, list) else file_info)

        return extract_bytes

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract_optional(files_map: dict[str, Any]) -> Any:
            if key not in files_map:
                return default_value

            file_info = files_map[key]

            if expects_upload_file:
                if isinstance(file_info, list):
                    uploads = [UploadFile.from_file_info(f) for f in file_info]
                    # Track for auto-cleanup
                    if "_upload_files" not in files_map:
                        files_map["_upload_files"] = []
                    files_map["_upload_files"].extend(uploads)
                    return uploads if expects_list else uploads[0]
                else:
                    upload = UploadFile.from_file_info(file_info)
                    # Track for auto-cleanup
                    if "_upload_files" not in files_map:
                        files_map["_upload_files"] = []
                    files_map["_upload_files"].append(upload)
                    return [upload] if expects_list else upload
            else:
                # Legacy behavior for dict annotations
                if expects_list and not isinstance(file_info, list):
                    return [file_info]
                return file_info

        return extract_optional
    else:

        def extract_required(files_map: dict[str, Any]) -> Any:
            if key not in files_map:
                raise RequestValidationError(
                    errors=[
                        {
                            "type": "file_missing",
                            "loc": ("body", key),
                            "msg": "Missing required file",
                            "input": None,
                        }
                    ]
                )

            file_info = files_map[key]

            if expects_upload_file:
                if isinstance(file_info, list):
                    uploads = [UploadFile.from_file_info(f) for f in file_info]
                    # Track for auto-cleanup
                    if "_upload_files" not in files_map:
                        files_map["_upload_files"] = []
                    files_map["_upload_files"].extend(uploads)
                    return uploads if expects_list else uploads[0]
                else:
                    upload = UploadFile.from_file_info(file_info)
                    # Track for auto-cleanup
                    if "_upload_files" not in files_map:
                        files_map["_upload_files"] = []
                    files_map["_upload_files"].append(upload)
                    return [upload] if expects_list else upload
            else:
                # Legacy behavior for dict annotations
                if expects_list and not isinstance(file_info, list):
                    return [file_info]
                return file_info

        return extract_required


_BODY_EXTRACTORS: dict[Any, Callable] = {}
# The extractors of optional bodies with the default None, by the extractor of the type.
_OPTIONAL_BODY_EXTRACTORS: dict[Callable, Callable] = {}


def create_body_extractor(
    name: str,
    annotation: Any,
    default: Any = inspect.Parameter.empty,
    embed_key: str | None = None,
) -> Callable:
    """
    Create a pre-compiled extractor for request body.

    With ``embed_key`` (``Body(embed=True)``), the body is a JSON object and
    the value is at that key. See ``_build_embedded_body_extractor``.

    Uses cached msgspec decoder for maximum performance.
    Converts msgspec.DecodeError (JSON parsing errors) to RequestValidationError for proper 422 responses.

    Each body type has one extractor. So a handler and a dependency with the
    same body type decode the body one time (see ``decode_body_once``).

    An empty body gives the default of a parameter that has one, and None for
    an ``Optional`` type without default. A required parameter gets a 422.
    """
    if embed_key is not None:
        return _build_embedded_body_extractor(annotation, embed_key, default)
    try:
        extractor = _BODY_EXTRACTORS[annotation]
    except KeyError:
        extractor = _BODY_EXTRACTORS[annotation] = _build_body_extractor(annotation)
    except TypeError:
        # An unhashable annotation gets its own extractor.
        extractor = _build_body_extractor(annotation)

    if default is inspect.Parameter.empty:
        if not is_optional(annotation):
            return extractor
        default = None
    if default is not None:
        return _with_empty_body_default(extractor, default)
    try:
        return _OPTIONAL_BODY_EXTRACTORS[extractor]
    except KeyError:
        optional = _OPTIONAL_BODY_EXTRACTORS[extractor] = _with_empty_body_default(extractor, None)
        return optional


def _with_empty_body_default(extractor: Callable, default_value: Any) -> Callable:
    def extract_or_default(body_bytes: bytes) -> Any:
        if not body_bytes:
            return default_value
        return extractor(body_bytes)

    return extract_or_default


def _build_embedded_body_extractor(annotation: Any, key: str, default: Any) -> Callable:
    """Build the extractor of a ``Body(embed=True)`` parameter.

    The body is a JSON object, and the value of the parameter is at ``key``:
    ``{"item": {...}}`` for the parameter ``item``. One Struct with one field
    decodes the object, so the request pays for one decode, as for a body with
    no wrapper. The other keys of the object are not read.

    A parameter with a default, or with an ``Optional`` type, takes the
    default when the key is missing or the request has no body.
    """
    if default is inspect.Parameter.empty and is_optional(annotation):
        default = None
    has_default = default is not inspect.Parameter.empty
    # The key is a field name on the wire only. It need not be a Python identifier.
    wire_field = msgspec.field(name=key, default_factory=lambda: default) if has_default else msgspec.field(name=key)
    wrapper = msgspec.defstruct("EmbeddedBody", [("value", annotation, wire_field)])
    decode = msgspec.json.Decoder(wrapper).decode

    def extract(body_bytes: bytes) -> Any:
        if has_default and not body_bytes:
            return default
        try:
            return decode(body_bytes).value
        except msgspec.ValidationError:
            # The error handler makes the 422. ValidationError is a subclass of DecodeError.
            raise
        except msgspec.DecodeError as e:
            error_detail = parse_msgspec_decode_error(e, body_bytes)
            raise RequestValidationError(errors=[error_detail], body=body_bytes) from e

    return extract


def _build_before_body_extractor(annotation: Any, plan: Callable) -> Callable:
    """The body extractor of a type that holds a serializer with a mode="before" validator."""

    def extract(body_bytes: bytes) -> Any:
        try:
            raw = msgspec.json.decode(body_bytes)
        except msgspec.DecodeError as e:
            raise RequestValidationError(
                errors=[parse_msgspec_decode_error(e, body_bytes)],
                body=body_bytes,
            ) from e
        data = run_before_validators(plan, raw)
        token = RAW_VALIDATED.set(True)
        try:
            # str_keys: the keys of a JSON object are strings.
            return msgspec.convert(data, annotation, str_keys=True)
        finally:
            RAW_VALIDATED.reset(token)

    return extract


def _build_body_extractor(annotation: Any) -> Callable:
    # Only a type with a before validator leaves the direct msgspec decode.
    plan = before_plan(annotation)
    if plan is not None:
        # The before path builds the same serializers, so it also ignores read_only input.
        return ignore_read_only_input(annotation, _build_before_body_extractor(annotation, plan))

    if is_msgspec_struct(annotation):
        decoder = get_msgspec_decoder(annotation)

        def extract(body_bytes: bytes) -> Any:
            try:
                return decoder.decode(body_bytes)
            except msgspec.ValidationError:
                # Re-raise ValidationError as-is (field validation errors handled by error_handlers.py)
                # IMPORTANT: Must catch ValidationError BEFORE DecodeError since ValidationError subclasses DecodeError
                raise
            except msgspec.DecodeError as e:
                # JSON parsing error (malformed JSON) - return 422 with error details including line/column
                error_detail = parse_msgspec_decode_error(e, body_bytes)
                raise RequestValidationError(
                    errors=[error_detail],
                    body=body_bytes,
                ) from e
    else:
        # Fallback to generic msgspec decode
        def extract(body_bytes: bytes) -> Any:
            try:
                return msgspec.json.decode(body_bytes, type=annotation)
            except msgspec.ValidationError:
                # Re-raise ValidationError as-is (field validation errors handled by error_handlers.py)
                # IMPORTANT: Must catch ValidationError BEFORE DecodeError since ValidationError subclasses DecodeError
                raise
            except msgspec.DecodeError as e:
                # JSON parsing error (malformed JSON) - return 422 with error details including line/column
                error_detail = parse_msgspec_decode_error(e, body_bytes)
                raise RequestValidationError(
                    errors=[error_detail],
                    body=body_bytes,
                ) from e

    # A body with no read_only serializer field keeps `extract` as it is.
    return ignore_read_only_input(annotation, extract)


def create_extractor_for_field(field: FieldDefinition, other_keys: frozenset[str] = frozenset()) -> Callable | None:
    """
    Create a pre-compiled extractor function for a FieldDefinition.

    This is the preferred factory that works with FieldDefinition objects.
    The returned function is specialized based on the parameter source,
    eliminating runtime type checking.

    Args:
        field: FieldDefinition object
        other_keys: The keys that the other parameters of the handler read from
            the source of this field. A query, cookie or form struct that
            forbids unknown fields does not get them.

    Returns:
        Extractor function, or None for 'request' and 'dependency' sources
        (which are handled specially by the injector)
    """
    source = field.source
    name = field.name
    annotation = field.annotation
    # An Optional type without default has the default None.
    default = None if field.default is inspect.Parameter.empty and is_optional(annotation) else field.default
    alias = field.alias

    # Return appropriate extractor based on source
    if source in _SINGLE_VALUE_SOURCES:
        if source == "path":
            extractor = create_path_extractor(name, annotation, alias)
        elif source == "header":
            extractor = create_header_extractor(name, annotation, default, alias)
        else:
            extractor = create_cookie_extractor(name, annotation, default, alias, other_keys)
        # After the extractor: a type that cannot hold one item gives its own error first.
        model = unwrap_optional(annotation)
        if is_msgspec_struct(model):
            for model_field in msgspec.structs.fields(model):
                _refuse_many_item_default(
                    source, model_field.encode_name, model_field.type, _schema_default(model_field)
                )
        else:
            _refuse_many_item_default(source, alias or name, annotation, default)
        return extractor
    elif source == "query":
        return create_query_extractor(name, annotation, default, alias, other_keys)
    elif source == "form":
        return create_form_extractor(name, annotation, default, alias, other_keys)
    elif source == "file":
        # Rust checks the file constraints of the Param from the route metadata.
        return create_file_extractor(name, annotation, default, alias)
    elif source == "body":
        # Body(embed=True): the value is at the key of the alias or of the parameter name.
        embed_key = (alias or name) if field.embed else None
        return create_body_extractor(name, annotation, default, embed_key)
    elif source == "request":
        # Request source is handled directly in the injector
        return None
    elif source == "dependency":
        # Dependencies are handled specially in the injector
        return None
    raise TypeError(
        f"Parameter {name!r} has an unsupported source {source!r}. "
        "Use one of: path, query, header, cookie, form, file, body."
    )


def compile_response_coercer(annotation: Any) -> tuple[Callable[[Any], Any], Callable[[Any], Any]]:
    """Build the response validation of one response type, once per route.

    A handler can return a dict, a msgspec Struct, or an object with
    attributes, such as a Django model instance. msgspec reads an object by
    attribute name, so a renamed field and a field default work as for a dict.

    Returns the sync coercion and its async form. They differ only for a
    QuerySet in a ``list[Struct]`` response: the async form loads it on the ORM
    executor.
    """
    if get_origin(annotation) is list:
        return _compile_list_coercer(annotation)
    if is_msgspec_struct(annotation):
        coerce = _compile_struct_coercer(annotation)
    else:
        coerce = _compile_other_coercer(annotation)

    async def coerce_async(value: Any) -> Any:
        return coerce(value)

    return coerce, coerce_async


def _compile_list_coercer(annotation: Any) -> tuple[Callable[[Any], Any], Callable[[Any], Any]]:
    args = get_args(annotation)
    item_type = args[0] if args else Any
    item_name = item_type.__name__ if isinstance(item_type, type) else repr(item_type)

    def coerce(value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            if value is None:
                return []
            raise TypeError(
                f"Response type mismatch: expected list[{item_name}], "
                f"but handler returned {type(value).__name__}. "
                f"Make sure your handler returns a list."
            )
        return msgspec.convert(value, annotation, from_attributes=True)

    # A QuerySet loads only the fields of the Struct, with .values(). A list of
    # Bolt Serializers does not get here: its own response handlers run.
    if not is_msgspec_struct(item_type):

        async def coerce_async(value: Any) -> Any:
            return coerce(value)

        return coerce, coerce_async

    load_rows, _ = compile_struct_row_readers(item_type)

    def coerce_with_queryset(value: Any) -> Any:
        if isinstance(value, QuerySet):
            return msgspec.convert(load_rows(value), annotation)
        return coerce(value)

    async def coerce_with_queryset_async(value: Any) -> Any:
        if isinstance(value, QuerySet):
            return msgspec.convert(await run_in_orm_executor(load_rows, value), annotation)
        return coerce(value)

    return coerce_with_queryset, coerce_with_queryset_async


def compile_struct_row_readers(
    item_type: type,
) -> tuple[Callable[[QuerySet], list[dict[str, Any]]], Callable[[list[Any]], list[dict[str, Any]]]]:
    """Build the row readers for a ``list[Struct]`` response, once per route.

    The first reader loads a QuerySet with ``.values()``, which reads only the
    fields of the Struct. The second reads objects, such as model instances,
    by attribute. Each row is keyed by the wire name of its field, so a field
    with ``rename=`` or ``msgspec.field(name=...)`` gets the name the Struct
    declares.
    """
    names = item_type.__struct_fields__
    wire_names = item_type.__struct_encode_fields__
    pairs = tuple(zip(names, wire_names, strict=True))

    def project(items: list[Any]) -> list[dict[str, Any]]:
        return [{wire: getattr(item, name, None) for name, wire in pairs} for item in items]

    select, read = compile_struct_row_steps(item_type)

    def load_rows(queryset: QuerySet) -> list[dict[str, Any]]:
        return read(select(queryset))

    if _has_nested_struct(item_type):
        return load_rows, _compile_nested_projector(item_type)
    return load_rows, project


def compile_struct_row_steps(
    item_type: type,
) -> tuple[Callable[[QuerySet], QuerySet], Callable[[QuerySet], list[dict[str, Any]]]]:
    """Build the two steps of the QuerySet reader of ``compile_struct_row_readers``, once per route.

    The first step gives the ``.values()`` QuerySet of the fields of the
    Struct. It sends no query. The second step reads the rows of that QuerySet
    and keys each row by the wire names. A caller can load the rows between
    the steps, for example on the event loop.

    A nested Struct names a foreign key or a one-to-one relation. ``.values()``
    reads its fields across the relation, so one query loads all rows.
    """
    if _has_nested_struct(item_type):
        plan_of = _compile_plan_getter(item_type)

        def select_nested(queryset: QuerySet) -> QuerySet:
            return queryset.values(*plan_of(queryset.model).paths)

        def read_nested(values: QuerySet) -> list[dict[str, Any]]:
            build = plan_of(values.model).build
            return [build(row) for row in values]

        return select_nested, read_nested

    names = item_type.__struct_fields__
    wire_names = item_type.__struct_encode_fields__

    def select(queryset: QuerySet) -> QuerySet:
        return queryset.values(*names)

    if names == wire_names:
        return select, list

    pairs = tuple(zip(names, wire_names, strict=True))

    def read(values: QuerySet) -> list[dict[str, Any]]:
        return [{wire: row[name] for name, wire in pairs} for row in values]

    return select, read


class _RowPlan(NamedTuple):
    """How Bolt reads a Struct with a nested Struct field from one model.

    A query names a reverse relation by its query name. A model instance
    names it by its accessor. The two names can differ, so the plan keeps
    a path of each kind.
    """

    paths: tuple[str, ...]
    """The ``.values()`` paths, with query names."""
    build: Callable[[dict[str, Any]], dict[str, Any]]
    """Nests a ``.values()`` row."""
    joins: tuple[str, ...]
    """The ``select_related()`` paths, with query names."""
    prefetches: tuple[str, ...]
    """The ``prefetch_related_objects()`` paths, with accessors."""
    read_instance: Callable[[Any], _InstanceView]
    """Gives the view of a model instance that msgspec reads."""
    needs_rows: bool
    """True if a reverse one-to-one relation can have no row. msgspec then reads views, not instances."""
    instance_error: str | None
    """Why a model instance cannot give the Struct, or None. See ``_reverse_relation``."""


def _field_value_type(annotation: Any) -> Any:
    """Return the type of a Struct field without ``| None`` and ``Annotated`` metadata."""
    annotation = unwrap_optional(annotation)
    if get_origin(annotation) is Annotated:
        annotation = unwrap_optional(get_args(annotation)[0])
    return annotation


def _nested_struct_type(annotation: Any) -> Any:
    """Return the Struct of a field typed ``Struct`` or ``list[Struct]``, or None."""
    value_type = _field_value_type(annotation)
    if get_origin(value_type) is list:
        args = get_args(value_type)
        value_type = _field_value_type(args[0]) if args else None
    return value_type if is_msgspec_struct(value_type) else None


def _has_nested_struct(item_type: type) -> bool:
    """Return True if a field of ``item_type`` holds a Struct or a list of Structs.

    A forward reference can resolve after the route registers. Such a Struct
    counts as nested: its readers resolve the fields on first use.
    """
    try:
        fields = msgspec.structs.fields(item_type)
    except NameError:
        return True
    return any(_nested_struct_type(field.type) is not None for field in fields)


def _compile_plan_getter(item_type: type) -> Callable[[type], _RowPlan]:
    """Return the function that gives the plan of ``item_type`` for a model.

    The relations depend on the model. Thus each model gets its plan on first
    use, and the plan is kept.
    """
    plans: dict[type, _RowPlan] = {}

    def plan_of(model: type) -> _RowPlan:
        plan = plans.get(model)
        if plan is None:
            # Benign race: two threads make the same plan.
            plan = plans[model] = _row_plan(model, item_type, "", "", (item_type,))
        return plan

    return plan_of


def _compile_nested_projector(item_type: type) -> Callable[[list[Any]], list[dict[str, Any]]]:
    """Build the reader of objects for a Struct with a nested Struct field, once per route.

    A field of a nested Struct that holds a model instance gives the row of
    that instance, not the instance. So does the view that
    :meth:`StructRelationLoader.rows` makes of an instance. A relation with
    no object gives None.
    """
    readers: dict[type, Callable[[Any], dict[str, Any]]] = {}

    def reader_of(struct_type: type) -> Callable[[Any], dict[str, Any]]:
        reader = readers.get(struct_type)
        if reader is not None:
            return reader
        fields = tuple(
            (field.name, field.encode_name, _nested_struct_type(field.type))
            for field in msgspec.structs.fields(struct_type)
        )

        def reader(item: Any) -> dict[str, Any]:
            row = {}
            for name, wire, nested_type in fields:
                value = getattr(item, name, None)
                if nested_type is not None and isinstance(value, (Model, _InstanceView)):
                    value = reader_of(nested_type)(value)
                row[wire] = value
            return row

        readers[struct_type] = reader
        return reader

    def project(items: list[Any]) -> list[dict[str, Any]]:
        read = reader_of(item_type)
        return [read(item) for item in items]

    return project


class StructRelationLoader:
    """Load the relations that a nested Struct names, before Bolt reads model instances.

    msgspec reads a model instance by attribute. A relation that is not loaded
    makes a query for each instance, and it cannot load on the event loop.
    The loader uses the relations of the ``.values()`` plan. Thus all
    instances of a page cost one query for each relation, or no query.
    """

    __slots__ = ("_plan_of",)

    def __init__(self, item_type: type) -> None:
        self._plan_of = _compile_plan_getter(item_type)

    def select_related(self, queryset: QuerySet) -> QuerySet:
        """Return ``queryset`` with a join for each relation, when the join is safe.

        A ``values()`` QuerySet gives no instances. A union cannot join.
        ``select_related()`` with no fields follows all relations: more fields
        would replace them. A deferred field can be a relation, which cannot
        join. Bolt does not change such a QuerySet: :meth:`load` loads the
        relations after the query.
        """
        query = queryset.query
        if (
            not issubclass(queryset._iterable_class, ModelIterable)
            or query.combinator
            or query.select_related is True
            or query.deferred_loading[0]
        ):
            return queryset
        joins = self._plan_of(queryset.model).joins
        return queryset.select_related(*joins) if joins else queryset

    def load(self, items: list[Any]) -> None:
        """Load the relations of the model instances in ``items``. Run it where sync ORM calls are safe.

        A relation that is already loaded makes no query. Each batch holds the
        instances of one model and one database: Django reads the database of
        the related rows from the first instance of a batch. The items stay
        model instances, so a paginator can read the cursor fields.
        """
        groups: dict[tuple[type, str | None], list[Model]] = {}
        for item in items:
            if isinstance(item, Model):
                groups.setdefault((type(item), item._state.db), []).append(item)
        for (model, _), instances in groups.items():
            plan = self._plan_of(model)
            if plan.instance_error is not None:
                raise TypeError(plan.instance_error)
            if plan.prefetches:
                prefetch_related_objects(instances, *plan.prefetches)

    async def aload(self, items: list[Any]) -> None:
        """Run :meth:`load` on the ORM executor, if ``items`` holds model instances."""
        if items and isinstance(items[0], Model):
            await run_in_orm_executor(self.load, items)

    def evaluate(self, queryset: QuerySet) -> list[Any]:
        """Evaluate ``queryset`` with its relations loaded."""
        items = list(self.select_related(queryset))
        self.load(items)
        return items

    def rows(self, items: list[Any]) -> list[Any]:
        """Return the items for msgspec, after :meth:`load`. It makes no query.

        An instance whose Struct nests a reverse one-to-one relation becomes an
        :class:`_InstanceView`: msgspec cannot read a relation with no row as
        None. Other items stay as they are.
        """
        if not items or not isinstance(items[0], Model):
            return items
        readers: dict[type, Callable[[Any], _InstanceView]] = {}
        for kind in {type(item) for item in items}:
            if issubclass(kind, Model) and (plan := self._plan_of(kind)).needs_rows:
                readers[kind] = plan.read_instance
        if not readers:
            return items
        return [item if (read := readers.get(type(item))) is None else read(item) for item in items]


def compile_struct_relation_loader(item_type: type) -> StructRelationLoader | None:
    """Return the relation loader of ``item_type``, or None if no field holds a Struct."""
    return StructRelationLoader(item_type) if _has_nested_struct(item_type) else None


class _Relation(NamedTuple):
    """A relation that a Struct field names."""

    field: Any
    """The model field, or the ``ForeignObjectRel`` of a reverse relation."""
    query_name: str
    """The name of the relation in a query."""
    accessor: str | None
    """The attribute of a model instance that gives the related object, or None if no attribute gives it."""


def _relation_of(model: type, name: str) -> _Relation | None:
    """Return the relation that the Struct field ``name`` names, or None.

    A Struct reads a model instance by attribute, so the accessor of a reverse
    relation comes first. Django names a reverse relation by its query name
    in a query. That name can also be the name of the field.
    """
    for rel in model._meta.related_objects:
        if rel.get_accessor_name() == name:
            return _reverse_relation(rel)
    try:
        field = model._meta.get_field(name)
    except FieldDoesNotExist:
        return None
    if not field.is_relation or field.related_model is None:
        return None
    if isinstance(field, ForeignObjectRel):
        return _reverse_relation(field)
    return _Relation(field, name, name)


def _reverse_relation(rel: ForeignObjectRel) -> _Relation:
    """Return the relation of ``rel``. Only a reverse one-to-one that is not hidden has an accessor.

    A hidden relation (``related_name="+"``) has no attribute on the instance.
    The attribute of a ``GenericRel`` (the query name of a ``GenericRelation``)
    gives a manager, not the related object. A reverse foreign key and a
    many-to-many relation have many rows, and ``_row_plan`` refuses them.
    """
    accessor = rel.get_accessor_name() if isinstance(rel, OneToOneRel) and not rel.hidden else None
    return _Relation(rel, rel.name, accessor)


def _row_plan(model: type, struct_type: type, prefix: str, attr_prefix: str, outer: tuple[type, ...]) -> _RowPlan:
    """Return the plan that reads ``struct_type`` from ``model``.

    A nested Struct field of a relation reads the relation itself first. A
    row with no related object gives None for the field. ``prefix`` and
    ``attr_prefix`` are the query path and the accessor path to ``model``.
    ``outer`` holds ``struct_type`` and the Structs that contain it.
    """
    paths: list[str] = []
    joins: list[str] = []
    prefetches: list[str] = []
    readers: list[tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]] | None]] = []
    # The reader of each relation field, by field name. The view of an
    # instance reads each other field from the instance itself.
    relations: dict[str, Callable[[Any], Any]] = {}
    needs_rows = False
    instance_error = None
    for field in msgspec.structs.fields(struct_type):
        nested_type = _nested_struct_type(field.type)
        relation = _relation_of(model, field.name) if nested_type is not None else None
        if relation is not None and (relation.field.one_to_many or relation.field.many_to_many):
            raise TypeError(
                f"{struct_type.__name__}.{field.name} is a relation with many rows. "
                "A plain Struct response cannot load it. "
                "Use a Bolt Serializer for this response: it loads the rows with prefetch_related()."
            )
        if relation is None or not is_msgspec_struct(_field_value_type(field.type)):
            path = prefix + field.name
            paths.append(path)
            readers.append((field.encode_name, path, None))
            continue
        if nested_type in outer:
            raise TypeError(
                f"{nested_type.__name__} nests itself across the relation {struct_type.__name__}.{field.name}. "
                "One .values() query cannot load it. Build the Structs in the handler and return them."
            )
        path = prefix + relation.query_name
        if relation.accessor is None:
            nested_plan = _row_plan(relation.field.related_model, nested_type, path + "__", "", (*outer, nested_type))
            paths.append(path)
            paths.extend(nested_plan.paths)
            readers.append((field.encode_name, path, nested_plan.build))
            kind = (
                "a hidden relation (related_name='+')"
                if relation.field.hidden
                else "the query name of a GenericRelation"
            )
            instance_error = instance_error or (
                f"{struct_type.__name__}.{field.name} is {kind}. No attribute of a model instance gives its row. "
                "Only a QuerySet response with no pagination reads it, with .values()."
            )
            continue
        attr_path = attr_prefix + relation.accessor
        nested_plan = _row_plan(
            relation.field.related_model, nested_type, path + "__", attr_path + "__", (*outer, nested_type)
        )
        paths.append(path)
        paths.extend(nested_plan.paths)
        joins.append(path)
        joins.extend(nested_plan.joins)
        prefetches.append(attr_path)
        prefetches.extend(nested_plan.prefetches)
        readers.append((field.encode_name, path, nested_plan.build))
        no_row: type[Exception] | tuple[()] = ()
        if isinstance(relation.field, OneToOneRel):
            no_row = getattr(model, relation.accessor).RelatedObjectDoesNotExist
            needs_rows = True
        needs_rows = needs_rows or nested_plan.needs_rows
        instance_error = instance_error or nested_plan.instance_error
        relations[field.name] = _relation_reader(relation.accessor, nested_plan.read_instance, no_row)

    def build(row: dict[str, Any]) -> dict[str, Any]:
        return {
            wire: row[path] if nested is None or row[path] is None else nested(row) for wire, path, nested in readers
        }

    return _RowPlan(
        tuple(paths),
        build,
        tuple(joins),
        tuple(prefetches),
        partial(_InstanceView, relations),
        needs_rows,
        instance_error,
    )


class _InstanceView:
    """A model instance as msgspec reads it, with the relation fields of a Struct read by the plan.

    msgspec reads each other field from the instance itself. Thus an
    attribute that raises has the same effect as without the view: msgspec
    uses the default of the field or reports the missing field. msgspec does
    not read a dict into an ``array_like`` Struct, so the view is not a dict.
    The slots have mangled names, so a field name cannot read them.
    """

    __slots__ = ("__instance", "__relations")

    def __init__(self, relations: dict[str, Callable[[Any], Any]], instance: Any) -> None:
        self.__instance = instance
        self.__relations = relations

    def __getattr__(self, name: str) -> Any:
        read = self.__relations.get(name)
        if read is None:
            return getattr(self.__instance, name)
        return read(self.__instance)


def _relation_reader(
    accessor: str, read_nested: Callable[[Any], _InstanceView], no_row: type[Exception] | tuple[()]
) -> Callable[[Any], Any]:
    """Build the reader of one relation field, once per model. The relation must be loaded already.

    ``no_row`` is the error of a reverse one-to-one with no row, which gives
    None. An empty tuple catches no error.
    """

    def read(instance: Any) -> Any:
        try:
            value = getattr(instance, accessor)
        except no_row:
            return None
        return None if value is None else read_nested(value)

    return read


def _compile_struct_coercer(annotation: type) -> Callable[[Any], Any]:
    name = annotation.__name__

    def coerce(value: Any) -> Any:
        if isinstance(value, annotation):
            return value
        if isinstance(value, (list, tuple)):
            raise TypeError(
                f"Response type mismatch: expected a single {name}, "
                f"but handler returned a list. Did you mean to annotate with list[{name}]?"
            )
        try:
            return msgspec.convert(value, annotation, from_attributes=True)
        except msgspec.ValidationError as e:
            # Keep the msgspec type. The error handler reads the path of the field from its message.
            raise msgspec.ValidationError(f"Response validation failed for {name}: {e}") from e

    return coerce


def _compile_other_coercer(annotation: Any) -> Callable[[Any], Any]:
    # A parametrized Struct, such as the pagination envelope
    # PaginatedResponse[Item], passes as it is when the handler built it.
    origin = get_origin(annotation)
    struct_origin = origin if is_msgspec_struct(origin) else None
    # isinstance() accepts a plain class, but not a generic, a union or Any.
    try:
        isinstance(None, annotation)
        plain_type = annotation
    except TypeError:
        plain_type = None

    def coerce(value: Any) -> Any:
        if struct_origin is not None and type(value) is struct_origin:
            return value
        if plain_type is not None and isinstance(value, plain_type):
            return value
        try:
            return msgspec.convert(value, annotation, from_attributes=True)
        except msgspec.ValidationError as e:
            raise TypeError(f"Response validation failed: {e}") from e

    return coerce
