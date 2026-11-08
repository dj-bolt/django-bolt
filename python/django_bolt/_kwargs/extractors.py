"""
Parameter binding and extraction with pre-compiled extractors.

This module provides high-performance parameter extraction using pre-compiled
extractor functions that avoid runtime type checking.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import Annotated, Any, get_args, get_origin

import msgspec
from django.db.models import QuerySet

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


def _param_converter(annotation: Any, loc: str, key: str, *, one_item: bool = False) -> Callable | None:
    """Build the conversion of one parameter value that Rust cannot do, or None.

    Rust converts a scalar value by its type. It does not build a sequence and
    it does not check ``msgspec.Meta`` constraints. For such a parameter, one
    ``msgspec.convert`` builds the ``list``, ``set``, ``frozenset`` or ``tuple``
    from the values and checks the constraints. A bad value gives a 422 that
    names ``loc`` and ``key``. With ``one_item``, a source that gives one value
    (a path, a header or a cookie) gives a sequence of that one item. A type
    that cannot hold one item, such as ``tuple[int, str]``, then raises
    ``TypeError`` here, when the route registers.
    """
    target = unwrap_optional(annotation)
    base = get_args(target)[0] if get_origin(target) is Annotated else target
    inner = unwrap_optional(base)
    # A bare `set` has no origin. msgspec reads it as set[Any].
    is_sequence = (get_origin(inner) or inner) in _SEQUENCE_ORIGINS
    constrained = get_origin(target) is Annotated and any(
        getattr(extra, name, None) is not None
        for extra in get_args(target)[1:]
        if isinstance(extra, msgspec.Meta)
        for name in _META_CONSTRAINTS
    )
    if not is_sequence and not constrained:
        return None
    wrap = is_sequence and one_item
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
        try:
            return msgspec.convert((value,), holder, strict=False).value
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


def create_query_extractor(name: str, annotation: Any, default: Any, alias: str | None = None) -> Callable:
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
        return _create_param_struct_extractor(unwrapped, default, "query parameter")

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


def create_cookie_extractor(name: str, annotation: Any, default: Any, alias: str | None = None) -> Callable:
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
        return _create_param_struct_extractor(unwrapped, default, "cookie")

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


def create_form_extractor(name: str, annotation: Any, default: Any, alias: str | None = None) -> Callable:
    """Create a pre-compiled extractor for form fields.

    Supports both individual fields and Struct/Serializer types.
    When annotation is a msgspec.Struct or Serializer, extracts all struct
    fields from the form data and constructs the struct instance.

    Note: Rust pre-converts values to typed Python objects (int, float, bool, str).
    """
    # Check if annotation is a Struct/Serializer type
    unwrapped = unwrap_optional(annotation)
    if is_msgspec_struct(unwrapped):
        return _create_param_struct_extractor(unwrapped, default, "form field")

    # Individual field extraction
    key = alias or name
    convert = _param_converter(annotation, "body", key)
    if convert is not None:
        return _converted_extractor(key, annotation, default, convert, f"Missing required form field: {key}")
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract(form_map: dict[str, Any]) -> Any:
            return form_map.get(key, default_value)
    else:

        def extract(form_map: dict[str, Any]) -> Any:
            if key not in form_map:
                raise HTTPException(status_code=422, detail=f"Missing required form field: {key}")
            return form_map[key]

    return extract


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

    Returns:
        list[dict[str, Any]]: A list of error dictionaries. Each dictionary contains:
            - "type": one of "validation_error", "missing_field", or "file_missing".
            - "loc": a tuple locating the error (e.g., ("query", "field") or ("body", "field")).
            - "msg": a human-readable message describing the issue.
            - "input": the offending input value or None for missing fields/files.
    """
    dec_hook = _upload_file_dec_hook if has_files else None
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
                        "input": data[encoded_name],
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

    Unwraps Optional[...] before checking the underlying origin. A bare ``set`` has no origin, so the class itself counts.

    Parameters:
        field_type (Any): The type annotation to inspect.

    Returns:
        bool: `True` if the (unwrapped) annotation's origin is one of list, set, frozenset, or tuple; `False` otherwise.
    """
    inner = unwrap_optional(field_type)
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


def _create_param_struct_extractor(struct_type: type, default: Any, param_type: str) -> Callable:
    """
    Create an extractor that builds and validates a msgspec Struct/Serializer from parameter data.

    The returned callable accepts parameter maps (and, when the struct has upload-file fields, an optional files map), converts the incoming values into an instance of `struct_type`, and performs validation. On validation failures the extractor raises RequestValidationError containing collected per-field errors. When the struct contains upload-file fields the extractor will also append created UploadFile instances to `files_map["_upload_files"]` for downstream cleanup.

    Behavioral notes:
    - Sequence-typed struct fields (e.g., list[T], tuple[T, ...], set[T]) accept single scalar wire values: scalars are wrapped into one-element lists before conversion.
    - The returned callable has an attribute `needs_files_map` set to True when upload-file fields are present (callable signature: (param_map, files_map | None) -> struct) and False otherwise (callable signature: (param_map) -> struct).

    Parameters:
        struct_type (type): msgspec struct/serializer type to produce.
        default (Any): Reserved for optional-struct support; not used by the extractor itself.
        param_type (str): The source name in error messages ("query parameter", "form field" or "cookie"). It also selects the source in `loc`.

    Returns:
        Callable: An extractor function that produces a validated instance of `struct_type` from incoming parameter data and raises RequestValidationError on validation errors.
    """
    # An unknown source fails here, when the route registers.
    source = _STRUCT_ERROR_SOURCES[param_type]
    # Check at registration time if any fields need file handling
    has_upload_file_fields = any(is_upload_file_type(f.type) for f in msgspec.structs.fields(struct_type))
    # Pre-compute sequence field names — empty tuple means zero runtime overhead.
    _seq_field_names = _collect_sequence_field_names(struct_type)

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

            # Wrap scalar values for list[T]-typed fields so a single
            # form occurrence still produces a one-element list.
            for name in _seq_field_names:
                value = merged.get(name)
                if value is not None and not isinstance(value, list):
                    merged[name] = [value]

            try:
                result = msgspec.convert(merged, struct_type, strict=False, dec_hook=_upload_file_dec_hook)
            except msgspec.ValidationError as e:
                errors = _collect_struct_errors(struct_type, merged, param_type, source, has_files=True)
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
        return extract_with_files
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

            try:
                return msgspec.convert(param_map, struct_type, strict=False)
            except msgspec.ValidationError as e:
                errors = _collect_struct_errors(struct_type, param_map, param_type, source)
                if errors:
                    raise RequestValidationError(errors=errors) from e
                raise

        extract.needs_files_map = False  # type: ignore[attr-defined]
        return extract


def _collect_header_struct_errors(
    header_fields: tuple[tuple[str, str, Any, bool], ...], data: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return one error for each missing or invalid field of a header struct.

    ``header_fields`` holds the header name, the encoded name, the type and the
    required flag of each field. ``data`` maps encoded names to header values.
    The ``loc`` of each error is ``("header", header name)``, as for a scalar header.
    """
    errors: list[dict[str, Any]] = []
    for header_name, encoded_name, field_type, required in header_fields:
        if encoded_name in data:
            try:
                msgspec.convert(data[encoded_name], type=field_type)
            except msgspec.ValidationError as e:
                errors.append(
                    {
                        "type": "validation_error",
                        "loc": ("header", header_name),
                        "msg": str(e),
                        "input": data[encoded_name],
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


def _create_header_struct_extractor(struct_type: type, default: Any) -> Callable:
    """Create an extractor that builds a Struct/Serializer from HTTP headers.

    Similar to _create_param_struct_extractor but converts field names from
    snake_case to kebab-case for HTTP header lookup. Supports msgspec field
    aliases (field(name=...)) and rename strategies.

    Note: Rust converts the typed struct fields before this runs (see
    ``create_header_extractor``), so msgspec gets typed values. A missing or
    invalid field gives a RequestValidationError with one error for each field.

    Args:
        struct_type: The msgspec.Struct or Serializer class
        default: Default value if the entire struct is optional

    Returns:
        Extractor function that takes headers_map and returns struct instance
    """
    _ = default  # Reserved for future optional struct support

    # Pre-compute header name mapping at registration time
    # Maps kebab-case header name → encoded field name (for msgspec alias support)
    header_to_encoded: list[tuple[str, str]] = []
    header_fields: list[tuple[str, str, Any, bool]] = []
    for field in msgspec.structs.fields(struct_type):
        encoded_name = getattr(field, "encode_name", field.name)
        header_name = encoded_name.lower().replace("_", "-")
        header_to_encoded.append((header_name, encoded_name))
        header_fields.append((header_name, encoded_name, field.type, field.required))
    error_fields = tuple(header_fields)

    def extract(headers_map: dict[str, str]) -> Any:
        converted = {}
        for header_name, encoded_name in header_to_encoded:
            value = headers_map.get(header_name)
            if value is not None:
                converted[encoded_name] = value

        try:
            return msgspec.convert(converted, struct_type)
        except msgspec.ValidationError as e:
            errors = _collect_header_struct_errors(error_fields, converted)
            if errors:
                raise RequestValidationError(errors=errors) from e
            raise

    return extract


def create_file_extractor(
    name: str,
    annotation: Any,
    default: Any,
    alias: str | None = None,
    max_size: int | None = None,  # Validated in Rust
    min_size: int | None = None,  # Validated in Rust
    allowed_types: Sequence[str] | None = None,  # Validated in Rust
    max_files: int | None = None,  # Validated in Rust
) -> Callable:
    """
    Create a pre-compiled extractor for file uploads with UploadFile support.

    Supports both the new UploadFile type and legacy dict annotations for
    backward compatibility.

    Note: File validation (max_size, min_size, allowed_types, max_files) is now
    handled in Rust at parse time. This function only creates UploadFile objects
    from the pre-validated file data.

    Args:
        name: Parameter name
        annotation: Type annotation (UploadFile, list[UploadFile], dict, list[dict])
        default: Default value
        alias: Alternative field name
        max_size: Maximum file size in bytes (validated in Rust)
        min_size: Minimum file size in bytes (validated in Rust)
        allowed_types: Allowed MIME types (validated in Rust)
        max_files: Maximum number of files (validated in Rust)

    Returns:
        Extractor function
    """
    # Silence unused parameter warnings - these are passed to Rust via metadata
    _ = max_size, min_size, allowed_types, max_files

    key = alias or name
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    # Determine expected type
    unwrapped = unwrap_optional(annotation)
    origin = get_origin(unwrapped)
    expects_list = origin is list

    # Check if we should create UploadFile instances
    # Use is_upload_file_type helper which handles all cases including Optional
    expects_upload_file = is_upload_file_type(annotation)

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
                # Legacy behavior for dict/bytes annotations
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
                # Legacy behavior for dict/bytes annotations
                if expects_list and not isinstance(file_info, list):
                    return [file_info]
                return file_info

        return extract_required


_BODY_EXTRACTORS: dict[Any, Callable] = {}


def create_body_extractor(name: str, annotation: Any) -> Callable:
    """
    Create a pre-compiled extractor for request body.

    Uses cached msgspec decoder for maximum performance.
    Converts msgspec.DecodeError (JSON parsing errors) to RequestValidationError for proper 422 responses.

    Each body type has one extractor. So a handler and a dependency with the
    same body type decode the body one time (see ``decode_body_once``).
    """
    try:
        return _BODY_EXTRACTORS[annotation]
    except KeyError:
        extractor = _BODY_EXTRACTORS[annotation] = _build_body_extractor(annotation)
        return extractor
    except TypeError:
        # An unhashable annotation gets its own extractor.
        return _build_body_extractor(annotation)


def _build_body_extractor(annotation: Any) -> Callable:
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

    return extract


def create_extractor_for_field(field: FieldDefinition) -> Callable | None:
    """
    Create a pre-compiled extractor function for a FieldDefinition.

    This is the preferred factory that works with FieldDefinition objects.
    The returned function is specialized based on the parameter source,
    eliminating runtime type checking.

    Args:
        field: FieldDefinition object

    Returns:
        Extractor function, or None for 'request' and 'dependency' sources
        (which are handled specially by the injector)
    """
    # Import here to avoid circular imports

    source = field.source
    name = field.name
    annotation = field.annotation
    default = field.default
    alias = field.alias

    # Return appropriate extractor based on source
    if source == "path":
        return create_path_extractor(name, annotation, alias)
    elif source == "query":
        return create_query_extractor(name, annotation, default, alias)
    elif source == "header":
        return create_header_extractor(name, annotation, default, alias)
    elif source == "cookie":
        return create_cookie_extractor(name, annotation, default, alias)
    elif source == "form":
        return create_form_extractor(name, annotation, default, alias)
    elif source == "file":
        # Extract file constraints from Param if available
        max_size = None
        min_size = None
        allowed_types = None
        max_files = None
        if field.param is not None:
            max_size = getattr(field.param, "max_size", None)
            min_size = getattr(field.param, "min_size", None)
            allowed_types = getattr(field.param, "allowed_types", None)
            max_files = getattr(field.param, "max_files", None)
        return create_file_extractor(
            name,
            annotation,
            default,
            alias,
            max_size=max_size,
            min_size=min_size,
            allowed_types=allowed_types,
            max_files=max_files,
        )
    elif source == "body":
        return create_body_extractor(name, annotation)
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
            raise TypeError(f"Response validation failed for {name}: {e}") from e

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
