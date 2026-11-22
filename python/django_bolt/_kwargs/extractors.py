"""
Parameter binding and extraction with pre-compiled extractors.

This module provides high-performance parameter extraction using pre-compiled
extractor functions that avoid runtime type checking.
"""

from __future__ import annotations

import datetime
import enum
import inspect
from collections.abc import Callable
from typing import Annotated, Any, Literal, get_args, get_origin

import msgspec
from django.db.models import QuerySet

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
    "compile_struct_row_readers",
    "get_msgspec_decoder",
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

    return convert


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
        if encoded_name in data:
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

    Unwraps Optional[...] and Annotated[...] before checking the underlying origin. A bare ``set`` has no origin, so the class itself counts.

    Parameters:
        field_type (Any): The type annotation to inspect.

    Returns:
        bool: `True` if the (unwrapped) annotation's origin is one of list, set, frozenset, or tuple; `False` otherwise.
    """
    inner = unwrap_optional(field_type)
    if get_origin(inner) is Annotated:
        inner = unwrap_optional(get_args(inner)[0])
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
            for name in _seq_field_names:
                value = merged.get(name)
                if value is not None and not isinstance(value, list):
                    merged[name] = [value]

            values = _encode_bytes_fields(merged, _bytes_fields) if _bytes_fields else merged
            try:
                result = msgspec.convert(values, struct_type, strict=False, dec_hook=_upload_file_dec_hook)
            except msgspec.ValidationError as e:
                errors = _collect_struct_errors(struct_type, values, param_type, source, has_files=True, inputs=merged)
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
            if _seq_field_names:
                # Only allocate a copy when there's actual wrapping to do.
                wrapped: dict[str, Any] | None = None
                for name in _seq_field_names:
                    value = param_map.get(name)
                    if value is not None and not isinstance(value, list):
                        if wrapped is None:
                            wrapped = dict(param_map)
                        wrapped[name] = [value]
                param_map = wrapped if wrapped is not None else param_map

            if _other_keys:
                # Only allocate a copy when the request has a key of another parameter.
                for name in _other_keys:
                    if name in param_map:
                        param_map = {k: v for k, v in param_map.items() if k not in _other_keys}
                        break

            values = _encode_bytes_fields(param_map, _bytes_fields) if _bytes_fields else param_map
            try:
                return msgspec.convert(values, struct_type, strict=False)
            except msgspec.ValidationError as e:
                errors = _collect_struct_errors(struct_type, values, param_type, source, inputs=param_map)
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
) -> list[dict[str, Any]]:
    """Return one error for each missing or invalid field of a header struct.

    ``header_fields`` holds the header name, the encoded name, the type and the
    required flag of each field. ``data`` maps encoded names to header values,
    with the UTF-8 bytes of a bytes field. ``inputs`` holds the values as the
    request gave them, for the ``input`` of an error.
    ``strict`` is the conversion mode of the extractor.
    The ``loc`` of each error is ``("header", header name)``, as for a scalar header.
    """
    errors: list[dict[str, Any]] = []
    for header_name, encoded_name, field_type, required in header_fields:
        if encoded_name in data:
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
    field converts with ``strict=False``, as the cookie struct does.

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

    def extract(headers_map: dict[str, str]) -> Any:
        converted = {}
        for header_name, encoded_name, wrap in header_to_encoded:
            value = headers_map.get(header_name)
            if value is not None:
                converted[encoded_name] = [value] if wrap else value

        values = _encode_bytes_fields(converted, bytes_fields) if bytes_fields else converted
        try:
            return msgspec.convert(values, struct_type, strict=strict)
        except msgspec.ValidationError as e:
            errors = _collect_header_struct_errors(error_fields, values, converted, strict)
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

    if names == wire_names:

        def load_rows(queryset: QuerySet) -> list[dict[str, Any]]:
            return list(queryset.values(*names))

    else:

        def load_rows(queryset: QuerySet) -> list[dict[str, Any]]:
            return [{wire: row[name] for name, wire in pairs} for row in queryset.values(*names)]

    def project(items: list[Any]) -> list[dict[str, Any]]:
        return [{wire: getattr(item, name, None) for name, wire in pairs} for item in items]

    return load_rows, project


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
