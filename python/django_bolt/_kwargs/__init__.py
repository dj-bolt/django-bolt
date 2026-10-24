"""
Handler kwargs compilation and extraction.

This module handles building kwargs for handler functions:
- extractors.py: Pre-compiled extractors (called once per field at startup)
- model.py: Handler model compilation (called once per route at startup)
- runtime.py: Runtime parameter extraction (called per request - hot path)
"""

from __future__ import annotations

from .extractors import (
    compile_response_coercer,
    create_body_extractor,
    create_cookie_extractor,
    create_extractor_for_field,
    create_file_extractor,
    create_form_extractor,
    create_header_extractor,
    create_path_extractor,
    create_query_extractor,
    get_msgspec_decoder,
)
from .model import (
    build_handler_arguments,
    classify_handler_pattern,
    compile_argument_injector,
    compile_binder,
    compile_websocket_binder,
    extract_response_metadata,
    field_has_upload_file,
)
from .runtime import extract_parameter_value, extract_path_params

__all__ = [
    # extractors.py
    "compile_response_coercer",
    "create_body_extractor",
    "create_cookie_extractor",
    "create_extractor_for_field",
    "create_file_extractor",
    "create_form_extractor",
    "create_header_extractor",
    "create_path_extractor",
    "create_query_extractor",
    "get_msgspec_decoder",
    # model.py
    "build_handler_arguments",
    "classify_handler_pattern",
    "compile_argument_injector",
    "compile_binder",
    "compile_websocket_binder",
    "extract_response_metadata",
    "field_has_upload_file",
    # runtime.py
    "extract_parameter_value",
    "extract_path_params",
]
