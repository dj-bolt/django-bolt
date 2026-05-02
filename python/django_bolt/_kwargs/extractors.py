"""
Parameter binding and extraction with pre-compiled extractors.

This module provides high-performance parameter extraction using pre-compiled
extractor functions that avoid runtime type checking.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import Any, get_args, get_origin

import msgspec
from asgiref.sync import sync_to_async

from ..datastructures import UploadFile
from ..exceptions import HTTPException, RequestValidationError, parse_msgspec_decode_error
from ..pagination import PaginatedResponse
from ..typing import (
    FieldDefinition,
    HandlerMetadata,
    is_msgspec_struct,
    is_optional,
    is_upload_file_type,
    unwrap_optional,
)

__all__ = [
    "create_extractor",
    "create_extractor_for_field",
    "create_path_extractor",
    "create_query_extractor",
    "create_header_extractor",
    "create_cookie_extractor",
    "create_form_extractor",
    "create_file_extractor",
    "create_body_extractor",
    "coerce_to_response_type",
    "coerce_to_response_type_async",
    "get_msgspec_decoder",
]


# Cache for msgspec decoders (performance optimization)
_DECODER_CACHE: dict[Any, msgspec.json.Decoder] = {}


def get_msgspec_decoder(type_: Any) -> msgspec.json.Decoder:
    """Get or create a cached msgspec decoder for a type."""
    if type_ not in _DECODER_CACHE:
        _DECODER_CACHE[type_] = msgspec.json.Decoder(type_)
    return _DECODER_CACHE[type_]


def create_path_extractor(name: str, annotation: Any, alias: str | None = None) -> Callable:
    """Create a pre-compiled extractor for path parameters.

    Note: Rust pre-converts values to typed Python objects (int, float, bool,
    str, uuid.UUID, decimal.Decimal, datetime, date, time) for both HTTP and WebSocket.
    """
    key = alias or name

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

    Note: Rust pre-converts values to typed Python objects (int, float, bool,
    str, uuid.UUID, decimal.Decimal, datetime, date, time) for both HTTP and WebSocket.
    """
    # Check if annotation is a Struct/Serializer type
    unwrapped = unwrap_optional(annotation)
    if is_msgspec_struct(unwrapped):
        return _create_header_struct_extractor(unwrapped, default)

    # Individual field extraction
    # Convert underscores to hyphens for HTTP header lookup
    # e.g., x_custom -> x-custom, content_type -> content-type
    key = (alias or name).lower().replace("_", "-")
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract(headers_map: dict[str, str]) -> Any:
            if key in headers_map:
                return headers_map[key]
            return default_value
    else:

        def extract(headers_map: dict[str, str]) -> Any:
            if key not in headers_map:
                raise HTTPException(status_code=422, detail=f"Missing required header: {key}")
            return headers_map[key]

    return extract


def create_cookie_extractor(name: str, annotation: Any, default: Any, alias: str | None = None) -> Callable:
    """Create a pre-compiled extractor for cookies.

    Supports both individual fields and Struct/Serializer types.
    When annotation is a msgspec.Struct or Serializer, extracts all struct
    fields from the cookies and constructs the struct instance.

    Note: Rust pre-converts values to typed Python objects (int, float, bool,
    str, uuid.UUID, decimal.Decimal, datetime, date, time) for both HTTP and WebSocket.
    """
    # Check if annotation is a Struct/Serializer type
    unwrapped = unwrap_optional(annotation)
    if is_msgspec_struct(unwrapped):
        return _create_param_struct_extractor(unwrapped, default, "cookie")

    # Individual field extraction
    key = alias or name
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract(cookies_map: dict[str, str]) -> Any:
            if key in cookies_map:
                return cookies_map[key]
            return default_value
    else:

        def extract(cookies_map: dict[str, str]) -> Any:
            if key not in cookies_map:
                raise HTTPException(status_code=422, detail=f"Missing required cookie: {key}")
            return cookies_map[key]

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
    has_files: bool = False,
) -> list[dict[str, Any]]:
    """Collect all validation errors by validating each field individually.

    msgspec.convert() is fail-fast (stops at first error). When it fails,
    we iterate fields one-by-one to collect ALL errors with proper types
    (e.g. "file_missing" vs "missing_field"). Based on the Litestar approach.
    """
    dec_hook = _upload_file_dec_hook if has_files else None
    loc_prefix = "body" if param_type == "form field" else param_type
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
                        "loc": (loc_prefix, encoded_name),
                        "msg": str(e),
                        "input": data[encoded_name],
                    }
                )
        elif field.required:
            is_file = is_upload_file_type(field.type)
            errors.append(
                {
                    "type": "file_missing" if is_file else "missing_field",
                    "loc": ("body", field.name) if is_file else (param_type, field.name),
                    "msg": "Missing required file" if is_file else f"Missing required {param_type}: {field.name}",
                    "input": None,
                }
            )
    return errors


def _create_param_struct_extractor(struct_type: type, default: Any, param_type: str) -> Callable:
    """Create an extractor that builds a Struct/Serializer from parameter data.

    Generic helper used by Form(), Query(), and Cookie() extractors.
    Uses msgspec.convert() which handles field aliases, rename strategies,
    defaults, and type validation. Falls back to field-by-field validation
    to collect all errors on failure.
    """
    # Check at registration time if any fields need file handling
    has_upload_file_fields = any(is_upload_file_type(f.type) for f in msgspec.structs.fields(struct_type))

    if has_upload_file_fields:
        _upload_field_names = [f.name for f in msgspec.structs.fields(struct_type) if is_upload_file_type(f.type)]

        def extract_with_files(param_map: dict[str, Any], files_map: dict[str, Any] | None = None) -> Any:
            files_map = files_map or {}
            merged = {**param_map}
            for k, v in files_map.items():
                if k != "_upload_files":
                    merged[k] = v

            try:
                result = msgspec.convert(merged, struct_type, strict=False, dec_hook=_upload_file_dec_hook)
            except msgspec.ValidationError as e:
                errors = _collect_struct_errors(struct_type, merged, param_type, has_files=True)
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
            try:
                return msgspec.convert(param_map, struct_type, strict=False)
            except msgspec.ValidationError as e:
                errors = _collect_struct_errors(struct_type, param_map, param_type)
                if errors:
                    raise RequestValidationError(errors=errors) from e
                raise

        extract.needs_files_map = False  # type: ignore[attr-defined]
        return extract


def _create_header_struct_extractor(struct_type: type, default: Any) -> Callable:
    """Create an extractor that builds a Struct/Serializer from HTTP headers.

    Similar to _create_param_struct_extractor but converts field names from
    snake_case to kebab-case for HTTP header lookup. Supports msgspec field
    aliases (field(name=...)) and rename strategies.

    Note: Rust pre-converts values to typed Python objects (int, float, bool, str).

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
    required_headers: set[str] = set()
    for field in msgspec.structs.fields(struct_type):
        encoded_name = getattr(field, "encode_name", field.name)
        header_name = encoded_name.lower().replace("_", "-")
        header_to_encoded.append((header_name, encoded_name))
        if field.required:
            required_headers.add(header_name)

    def extract(headers_map: dict[str, str]) -> Any:
        converted = {}
        for header_name, encoded_name in header_to_encoded:
            if header_name in headers_map:
                converted[encoded_name] = headers_map[header_name]
            elif header_name in required_headers:
                raise HTTPException(status_code=422, detail=f"Missing required header: {header_name}")

        return msgspec.convert(converted, struct_type)

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


def create_body_extractor(name: str, annotation: Any) -> Callable:
    """
    Create a pre-compiled extractor for request body.

    Uses cached msgspec decoder for maximum performance.
    Converts msgspec.DecodeError (JSON parsing errors) to RequestValidationError for proper 422 responses.
    """
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
    else:
        # Fallback for unknown sources
        if default is not inspect.Parameter.empty:
            return lambda *_args, **_kwargs: default
        return None


def create_extractor(field: dict[str, Any]) -> Callable:
    """
    Create an optimized extractor function for a parameter field.

    This is a factory that returns a specialized extractor based on the
    parameter source. The returned function is optimized to avoid runtime
    type checking.

    Args:
        field: Field metadata dictionary

    Returns:
        Extractor function that takes request data and returns parameter value
    """
    source = field["source"]
    name = field["name"]
    annotation = field["annotation"]
    default = field["default"]
    alias = field.get("alias")

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
        return create_file_extractor(name, annotation, default, alias)
    elif source == "body":
        return create_body_extractor(name, annotation)
    elif source == "request":
        # Request object is passed through directly
        return lambda request: request
    else:
        # Fallback for unknown sources
        def extract(*args, **kwargs):
            if default is not inspect.Parameter.empty:
                return default
            raise ValueError(f"Cannot extract parameter {name} with source {source}")

        return extract


async def coerce_to_response_type_async(value: Any, annotation: Any, meta: HandlerMetadata | None = None) -> Any:
    """
    Async version that handles Django QuerySets.

    Args:
        value: Value to coerce
        annotation: Target type annotation
        meta: Handler metadata with pre-computed serialization info

    Returns:
        Coerced value
    """
    # Check if value is a QuerySet AND we have pre-computed field names
    if meta and "response_field_names" in meta and hasattr(value, "_iterable_class") and hasattr(value, "model"):
        # Use pre-computed field names (computed at route registration time)
        field_names = meta["response_field_names"]

        # Call .values() to get a ValuesQuerySet
        values_qs = value.values(*field_names)

        # Convert QuerySet to list (this triggers SQL execution)
        # Using sync_to_thread(list) is MUCH faster than async for iteration
        #
        # Django's async for implementation (django/db/models/query.py:54-68):
        #   - Uses GET_ITERATOR_CHUNK_SIZE = 100 (django/db/models/sql/constants.py:7)
        #   - Calls sync_to_async for EACH chunk: `await sync_to_async(next_slice)(sync_generator)`
        #   - For 10,000 items: 100 sync_to_async calls (~30-50ms overhead)
        #   - For 100,000 items: 1000 sync_to_async calls (~300-500ms overhead)
        #
        # Our approach: 1 sync_to_thread call total (minimal overhead)
        # Performance gain: 100-1000x fewer context switches
        #
        # Memory tradeoff:
        #   - Paginated APIs (20-100 items/page): Trivial memory usage (~20-100KB)
        #   - Small lists (<10K items): Acceptable memory usage (<10MB)
        #   - Large unpaginated lists: Should use pagination or StreamingResponse + .iterator()
        items = await sync_to_async(list)(values_qs)

        # Let msgspec validate and convert entire list in one batch (much faster than N individual conversions)
        result = msgspec.convert(items, annotation)

        return result

    return coerce_to_response_type(value, annotation, meta)


def coerce_to_response_type(value: Any, annotation: Any, meta: HandlerMetadata | None = None) -> Any:
    """
    Coerce arbitrary Python objects (including Django models) into the
    declared response type using msgspec.

    Supports:
      - msgspec.Struct: build mapping from attributes if needed
      - list[T]: recursively coerce elements
      - dict/primitive: defer to msgspec.convert
      - Django QuerySet: convert to list using .values()

    Args:
        value: Value to coerce
        annotation: Target type annotation
        meta: Handler metadata with pre-computed type info

    Returns:
        Coerced value
    """
    # Skip validation for PaginatedResponse - pagination decorator handles serialization
    # PaginatedResponse is a msgspec.Struct that serializes directly
    if isinstance(value, PaginatedResponse):
        return value

    # Handle Django QuerySets - convert to list using .values()
    # Works for both sync and async handlers (sync handlers run in thread pool)
    if meta and "response_field_names" in meta and hasattr(value, "_iterable_class") and hasattr(value, "model"):
        # Use pre-computed field names (computed at route registration time)
        field_names = meta["response_field_names"]

        # Call .values() to get a ValuesQuerySet
        values_qs = value.values(*field_names)

        # Convert QuerySet to list (this triggers SQL execution)
        items = list(values_qs)

        # Let msgspec validate and convert entire list in one batch
        result = msgspec.convert(items, annotation)

        return result

    # Fast path: if annotation is a primitive type (dict, list, str, int, etc.),
    # just return the value without validation. Validation only makes sense for
    # structured types like msgspec.Struct or parameterized generics.
    # Handle both the actual type AND string annotations (PEP 563)
    if annotation in (dict, list, str, int, float, bool, bytes, bytearray, type(None)) or annotation in (
        "dict",
        "list",
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "bytearray",
        "None",
    ):
        # These are primitive types - no validation needed, return as-is
        return value

    # Use pre-computed type information if available
    if meta and "response_field_names" in meta:
        # This is a list[Struct] response - use pre-computed field names
        origin = get_origin(annotation)

        # Handle list[T]
        if origin is list:
            # Check if value is actually a list/iterable
            if not isinstance(value, (list, tuple)) and value is not None:
                args = get_args(annotation)
                elem_name = args[0].__name__ if args else "Any"
                raise TypeError(
                    f"Response type mismatch: expected list[{elem_name}], "
                    f"but handler returned {type(value).__name__}. "
                    f"Make sure your handler returns a list."
                )

            # Convert objects to dicts if needed (for custom objects that aren't dicts/structs)
            # Check first item to determine if conversion is needed
            if value and len(value) > 0:
                first_item = value[0]
                # If it's not a dict or a msgspec.Struct, convert objects to dicts
                if not isinstance(first_item, (dict, msgspec.Struct)):
                    field_names = meta["response_field_names"]
                    value = [{name: getattr(item, name, None) for name in field_names} for item in value]

            # For list of structs, we can use batch conversion with msgspec
            # This is much faster than iterating and converting one by one
            return msgspec.convert(value or [], annotation)

    # Fast path: if value is already the right type, return it
    # Cannot use isinstance() with parameterized generics (list[Item], dict[str, int], etc.)
    # Only check for non-generic types
    try:
        if isinstance(value, annotation):
            return value
    except TypeError:
        # annotation is a parameterized generic, skip the fast path
        pass

    # Handle Struct without metadata (single object, not list)
    if is_msgspec_struct(annotation):
        # Check for common type mismatches before msgspec validation
        if isinstance(value, (list, tuple)):
            raise TypeError(
                f"Response type mismatch: expected a single {annotation.__name__}, "
                f"but handler returned a list. Did you mean to annotate with list[{annotation.__name__}]?"
            )

        if isinstance(value, dict):
            try:
                return msgspec.convert(value, annotation)
            except msgspec.ValidationError as e:
                raise TypeError(f"Response validation failed for {annotation.__name__}: {e}") from e

        # Build mapping from attributes - use pre-computed field names if available
        if meta and "response_field_names" in meta:
            field_names = meta["response_field_names"]
        else:
            # Fallback: runtime introspection (slower)
            field_names = list(getattr(annotation, "__annotations__", {}).keys())

        mapped = {name: getattr(value, name, None) for name in field_names}
        try:
            return msgspec.convert(mapped, annotation)
        except msgspec.ValidationError as e:
            raise TypeError(f"Response validation failed for {annotation.__name__}: {e}") from e

    # Fallback: Check if it's a list without metadata
    origin = get_origin(annotation)
    if origin is list:
        if not isinstance(value, (list, tuple)) and value is not None:
            args = get_args(annotation)
            elem_name = args[0].__name__ if args else "Any"
            raise TypeError(
                f"Response type mismatch: expected list[{elem_name}], "
                f"but handler returned {type(value).__name__}. "
                f"Make sure your handler returns a list."
            )
        # Use msgspec batch conversion
        return msgspec.convert(value or [], annotation)

    # Default convert path
    try:
        return msgspec.convert(value, annotation)
    except msgspec.ValidationError as e:
        raise TypeError(f"Response validation failed: {e}") from e
