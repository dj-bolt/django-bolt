"""
Runtime parameter extraction functions.

This module contains functions called during request handling (hot path).
Keeping them as module-level functions allows:
- Direct bytecode CALL instruction (no LOAD_ATTR for self)
- No class instance overhead
- Potential inlining by PyPy/Cython
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import msgspec

from ..exceptions import HTTPException, RequestValidationError, parse_msgspec_decode_error
from ..typing import FieldDefinition, HandlerMetadata, is_msgspec_struct
from .extractors import get_msgspec_decoder

# Pre-compiled regex pattern for extracting path parameters
# Matches both {param} and {param:type} formats (e.g., {path:path} for catch-all)
PATH_PARAM_REGEX = re.compile(r"\{(\w+)(?::\w+)?\}")


def extract_path_params(path: str) -> set[str]:
    """
    Extract path parameter names from a route pattern.

    Examples:
        "/users/{user_id}" -> {"user_id"}
        "/posts/{post_id}/comments/{comment_id}" -> {"post_id", "comment_id"}
        "/files/{path:path}" -> {"path"}  # catch-all path parameter
    """
    return set(PATH_PARAM_REGEX.findall(path))


def extract_parameter_value(
    field: FieldDefinition,
    request: dict[str, Any],
    params_map: dict[str, Any],
    query_map: dict[str, Any],
    headers_map: dict[str, str],
    cookies_map: dict[str, str],
    form_map: dict[str, Any],
    files_map: dict[str, Any],
    meta: HandlerMetadata,
    body_obj: Any,
    body_loaded: bool,
) -> tuple[Any, Any, bool]:
    """
    Extract value for a handler parameter using FieldDefinition.

    Args:
        field: FieldDefinition object describing the parameter
        request: Request dictionary
        params_map: Path parameters
        query_map: Query parameters
        headers_map: Request headers
        cookies_map: Request cookies
        form_map: Form data
        files_map: Uploaded files
        meta: Handler metadata
        body_obj: Cached body object
        body_loaded: Whether body has been loaded

    Returns:
        Tuple of (value, body_obj, body_loaded)
    """
    name = field.name
    default = field.default
    source = field.source
    alias = field.alias
    key = alias or name

    # Handle different sources
    # Note: Rust pre-converts values to typed Python objects (int, float, bool, str)
    if source == "path":
        if key in params_map:
            return params_map[key], body_obj, body_loaded
        raise HTTPException(status_code=422, detail=f"Missing required path parameter: {key}")

    elif source == "query":
        if key in query_map:
            return query_map[key], body_obj, body_loaded
        elif field.is_optional:
            return (None if default is inspect.Parameter.empty else default), body_obj, body_loaded
        raise HTTPException(status_code=422, detail=f"Missing required query parameter: {key}")

    elif source == "header":
        lower_key = key.lower()
        if lower_key in headers_map:
            return headers_map[lower_key], body_obj, body_loaded
        elif field.is_optional:
            return (None if default is inspect.Parameter.empty else default), body_obj, body_loaded
        raise HTTPException(status_code=422, detail=f"Missing required header: {key}")

    elif source == "cookie":
        if key in cookies_map:
            return cookies_map[key], body_obj, body_loaded
        elif field.is_optional:
            return (None if default is inspect.Parameter.empty else default), body_obj, body_loaded
        raise HTTPException(status_code=422, detail=f"Missing required cookie: {key}")

    elif source == "form":
        if key in form_map:
            return form_map[key], body_obj, body_loaded
        elif field.is_optional:
            return (None if default is inspect.Parameter.empty else default), body_obj, body_loaded
        raise HTTPException(status_code=422, detail=f"Missing required form field: {key}")

    elif source == "file":
        if key in files_map:
            file_info = files_map[key]
            # Use pre-computed type properties from FieldDefinition (no runtime introspection)
            unwrapped_type = field.unwrapped_annotation
            origin = field.origin

            if unwrapped_type is bytes:
                # For bytes annotation, extract content from single file
                if isinstance(file_info, list):
                    # Multiple files, but bytes expects single - take first
                    return file_info[0].get("content", b""), body_obj, body_loaded
                return file_info.get("content", b""), body_obj, body_loaded
            elif origin is list:
                # For list annotation, ensure value is a list
                if isinstance(file_info, list):
                    return file_info, body_obj, body_loaded
                else:
                    # Wrap single file in list
                    return [file_info], body_obj, body_loaded
            else:
                # Return full file info for dict/Any annotations
                if isinstance(file_info, list):
                    # List but annotation doesn't expect list - take first
                    return file_info[0], body_obj, body_loaded
                return file_info, body_obj, body_loaded
        elif field.is_optional:
            return (None if default is inspect.Parameter.empty else default), body_obj, body_loaded
        raise HTTPException(status_code=422, detail=f"Missing required file: {key}")

    elif source == "body":
        # Handle body parameter
        if meta.get("body_struct_param") == name:
            if not body_loaded:
                body_bytes: bytes = request["body"]
                if is_msgspec_struct(meta["body_struct_type"]):
                    decoder = get_msgspec_decoder(meta["body_struct_type"])
                    try:
                        value = decoder.decode(body_bytes)
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
                    try:
                        value = msgspec.json.decode(body_bytes, type=meta["body_struct_type"])
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
                return value, value, True
            else:
                return body_obj, body_obj, body_loaded
        else:
            if field.is_optional:
                return (None if default is inspect.Parameter.empty else default), body_obj, body_loaded
            raise HTTPException(status_code=422, detail=f"Missing required parameter: {name}")

    else:
        # Unknown source
        if field.is_optional:
            return (None if default is inspect.Parameter.empty else default), body_obj, body_loaded
        raise HTTPException(status_code=422, detail=f"Missing required parameter: {name}")
