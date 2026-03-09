"""
Type introspection and field definition system for parameter binding.

Inspired by Litestar's architecture but built from scratch for Django-Bolt's
msgspec-first, async-only design with focus on performance.
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable
from dataclasses import dataclass, is_dataclass
from enum import Enum
from functools import reduce
from operator import or_
from typing import TYPE_CHECKING, Any, TypedDict, Union, get_args, get_origin

import msgspec

# Import Param and Depends at top level for use in from_parameter method
# These imports must be at module top to comply with PLC0415
from .datastructures import UploadFile
from .params import Depends as DependsMarker
from .params import Param

if TYPE_CHECKING:
    pass

__all__ = [
    "FieldDefinition",
    "HandlerMetadata",
    "HandlerPattern",
    "is_msgspec_struct",
    "is_simple_type",
    "is_sequence_type",
    "is_optional",
    "is_upload_file_type",
    "unwrap_optional",
    "infer_param_source",
]


class HandlerPattern(Enum):
    """
    Handler pattern classification for specialized injector selection.

    Each pattern enables a specific fast path in the argument injector,
    eliminating unnecessary checks and data access at request time.
    """

    REQUEST_ONLY = "request_only"  # Single request parameter
    NO_PARAMS = "no_params"  # No parameters at all
    PATH_ONLY = "path_only"  # Only path parameters
    QUERY_ONLY = "query_only"  # Only query parameters
    BODY_ONLY = "body_only"  # Single JSON body parameter
    SIMPLE = "simple"  # Path + query combination
    WITH_DEPS = "with_deps"  # Has dependency injection (async)
    FULL = "full"  # Complex: headers, cookies, form, file, or mixed


class HandlerMetadata(TypedDict, total=False):
    """
    Type-safe metadata dictionary for handler functions.

    This structure is compiled once at route registration time and
    contains all information needed for parameter binding, response
    serialization, and OpenAPI documentation generation.
    """

    # Core function metadata
    sig: inspect.Signature
    """Function signature"""

    fields: list[FieldDefinition]
    """List of parameter field definitions"""

    path_params: set[str]
    """Set of path parameter names extracted from route pattern"""

    http_method: str
    """HTTP method (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS)"""

    mode: str
    """Handler mode: 'request_only' or 'mixed'"""

    # Body parameter metadata (for fast path optimization)
    body_struct_param: str
    """Name of the single body struct parameter (if present)"""

    body_struct_type: Any
    """Type of the body struct parameter"""

    # Response metadata
    response_type: Any
    """Return type annotation from function signature"""

    default_status_code: int
    """Default HTTP status code for successful responses"""

    validate_response: bool
    """Whether schema-driven response validation/coercion runs at runtime"""

    # QuerySet serialization optimization (pre-computed at registration)
    response_field_names: list[str]
    """Pre-computed field names for QuerySet.values() call"""

    # Multi-response (per-status-code) metadata
    response_map: dict[int | type, Any]
    """Status code → response type mapping (when response_model is a dict).
    Keys are int status codes or ``...`` (Ellipsis) for catch-all."""

    response_field_names_map: dict[int, list[str]]
    """Per-status-code QuerySet field names"""

    is_multi_response: bool
    """Whether this handler uses per-status-code response schemas"""

    _resolved_metas: dict[int | type, dict]
    """Pre-built per-status-code meta dicts for O(1) lookup at request time.
    Keys mirror ``response_map`` (int codes + optional ``...``)."""

    _response_validator: Callable[[Any], Any] | None
    """Compiled sync response validator/coercer"""

    _response_validator_async: Callable[[Any], Any] | None
    """Compiled async response validator/coercer"""

    _stream_item_validator: Callable[[Any], Any] | None
    """Compiled sync validator for typed stream items"""

    _stream_item_validator_async: Callable[[Any], Any] | None
    """Compiled async validator for typed stream items"""

    _queryset_serializer: Callable[[Any], Any] | None
    """Compiled sync queryset projection serializer"""

    _queryset_serializer_async: Callable[[Any], Any] | None
    """Compiled async queryset projection serializer"""

    _has_response_validation: bool
    """Whether this route has a compiled response validator"""

    _default_response_handler: Callable[..., Any]
    """Compiled async data-response handler"""

    _default_response_handler_sync: Callable[..., Any]
    """Compiled sync data-response handler"""

    _response_type_handler: Callable[..., Any]
    """Compiled async explicit-response handler"""

    _response_type_handler_sync: Callable[..., Any]
    """Compiled sync explicit-response handler"""

    # Performance optimizations
    needs_form_parsing: bool
    """Whether this handler needs form/multipart parsing (Form/File params)"""

    # Sync/async handler metadata
    is_async: bool
    """Whether handler is async (coroutine function)"""

    # OpenAPI documentation metadata
    openapi_tags: list[str]
    """OpenAPI tags for grouping endpoints"""

    openapi_summary: str
    """Short summary for OpenAPI docs"""

    openapi_description: str
    """Detailed description for OpenAPI docs"""

    # Performance optimization: pre-compiled argument injector
    injector: Any
    """Pre-compiled function that extracts handler arguments from request.

    Returns (args, kwargs) tuple ready for handler invocation.
    Compiled once at route registration time for maximum performance.
    Can be sync (for most handlers) or async (only if using dependencies).

    Example:
        if meta["injector_is_async"]:
            args, kwargs = await meta["injector"](request)
        else:
            args, kwargs = meta["injector"](request)
        result = await handler(*args, **kwargs)
    """

    injector_is_async: bool
    """Whether the injector function is async (True only if handler uses Depends)"""

    # Static analysis flags (skip unused parsing)
    # These are computed at route registration time by analyzing handler parameters
    needs_body: bool
    """Whether handler needs request body parsing (has body/form/file params)"""

    needs_query: bool
    """Whether handler needs query string parsing (has query params)"""

    needs_headers: bool
    """Whether handler needs header extraction (has header params)"""

    needs_cookies: bool
    """Whether handler needs cookie parsing (has cookie params)"""

    needs_path_params: bool
    """Whether handler needs path parameter extraction (has path params)"""

    # Static route optimization
    is_static_route: bool
    """Whether route has no path parameters (can use O(1) lookup)"""

    # ORM analysis (static analysis for blocking detection)
    is_blocking: bool
    """Whether handler is likely to block (ORM usage or blocking I/O) - runs in thread pool"""

    # Handler pattern classification (for specialized fast paths)
    handler_pattern: HandlerPattern
    """Handler pattern for specialized injector selection at registration time."""


# Simple scalar types that map to query parameters
SIMPLE_TYPES = (str, int, float, bool, bytes)


def is_msgspec_struct(annotation: Any) -> bool:
    """Check if type is a msgspec.Struct."""
    try:
        return isinstance(annotation, type) and issubclass(annotation, msgspec.Struct)
    except (TypeError, AttributeError):
        return False


def is_simple_type(annotation: Any) -> bool:
    """Check if annotation is a simple scalar type (str, int, float, bool, bytes)."""
    origin = get_origin(annotation)
    if origin is not None:
        # Unwrap Optional, List, etc.
        return False
    return annotation in SIMPLE_TYPES or annotation is Any


def is_sequence_type(annotation: Any) -> bool:
    """Check if annotation is a sequence type like List[T]."""
    origin = get_origin(annotation)
    return origin in (list, tuple, set, frozenset)


def is_optional(annotation: Any) -> bool:
    """Check if annotation is Optional[T] or T | None."""
    origin = get_origin(annotation)
    # Handle both typing.Union (Optional[T]) and types.UnionType (T | None)
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        return type(None) in args
    return False


def is_upload_file_type(annotation: Any) -> bool:
    """
    Check if annotation is UploadFile or list[UploadFile].

    Used to detect file upload fields in Form() structs.

    Args:
        annotation: Type annotation to check

    Returns:
        True if annotation is UploadFile or list[UploadFile]
    """
    # Unwrap Optional first
    unwrapped = unwrap_optional(annotation)

    # Direct UploadFile check using type identity
    if unwrapped is UploadFile:
        return True

    # list[UploadFile]
    origin = get_origin(unwrapped)
    if origin is list:
        args = get_args(unwrapped)
        if args and args[0] is UploadFile:
            return True

    return False


def unwrap_optional(annotation: Any) -> Any:
    """Unwrap Optional[T] or T | None to get T."""
    origin = get_origin(annotation)
    # Handle both typing.Union (Optional[T]) and types.UnionType (T | None)
    if origin is Union or origin is types.UnionType:
        args = tuple(a for a in get_args(annotation) if a is not type(None))
        return args[0] if len(args) == 1 else reduce(or_, args)  # type: ignore
    return annotation


def is_dataclass_type(annotation: Any) -> bool:
    """Check if annotation is a dataclass."""
    try:
        return is_dataclass(annotation)
    except (TypeError, AttributeError):
        return False


def infer_param_source(name: str, annotation: Any, path_params: set[str], http_method: str) -> str:
    """
    Infer parameter source based on type and context.

    Inference rules (in priority order):
    1. If name matches path parameter -> "path"
    2. If special name (request, req) -> "request"
    3. If simple type (str, int, float, bool) -> "query"
    4. If msgspec.Struct or dataclass -> "body" (if method allows body)
    5. Default -> "query"

    Args:
        name: Parameter name
        annotation: Type annotation
        path_params: Set of path parameter names from route pattern
        http_method: HTTP method (GET, POST, etc.)

    Returns:
        Source string: "path", "query", "body", "request", etc.
    """
    # 1. Path parameters
    if name in path_params:
        return "path"

    # 2. Special request parameter
    if name in {"request", "req"}:
        return "request"

    # Unwrap Optional if present
    unwrapped = unwrap_optional(annotation)

    # 3. Simple types -> query params
    if is_simple_type(unwrapped):
        return "query"

    # 4. Sequence of simple types -> query (for list params)
    if is_sequence_type(unwrapped):
        args = get_args(unwrapped)
        if args and is_simple_type(args[0]):
            return "query"

    # 5. Complex types (msgspec.Struct, dataclass) -> body (if allowed)
    if is_msgspec_struct(unwrapped) or is_dataclass_type(unwrapped):
        if http_method in {"POST", "PUT", "PATCH"}:
            return "body"
        # For GET/DELETE/HEAD, this will trigger validation error later
        return "body"

    # 6. Default to query
    return "query"


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    """
    Represents a parsed function parameter with type metadata.

    This is the core data structure for parameter binding, containing
    all information needed to extract and validate a parameter value
    from an HTTP request.
    """

    name: str
    """Parameter name"""

    annotation: Any
    """Raw type annotation"""

    default: Any
    """Default value (inspect.Parameter.empty if required)"""

    source: str
    """Parameter source: 'path', 'query', 'body', 'header', 'cookie', 'form', 'file', 'request', 'dependency'"""

    alias: str | None = None
    """Alternative name for the parameter (e.g., 'user-id' for 'user_id')"""

    embed: bool | None = None
    """For body params: whether to embed in a wrapper object"""

    dependency: Any = None
    """Depends marker for dependency injection"""

    kind: inspect._ParameterKind = inspect.Parameter.POSITIONAL_OR_KEYWORD
    """Parameter kind (positional, keyword-only, etc.)"""

    param: Any = None
    """Original Param marker (for accessing file constraints, etc.)"""

    # Pre-compiled extractor function (set at registration time)
    extractor: Callable[..., Any] | None = None
    """Pre-compiled extractor function for this parameter.

    Created at route registration time by the appropriate factory
    (create_path_extractor, create_query_extractor, etc.).
    Eliminates per-request source type checking.
    """

    # Cached type properties for performance
    _is_optional: bool | None = None
    _is_simple: bool | None = None
    _is_struct: bool | None = None
    _unwrapped: Any | None = None
    _origin: Any | None = None

    @property
    def is_optional(self) -> bool:
        """Check if parameter is optional (has default or Optional type)."""
        if self._is_optional is None:
            object.__setattr__(
                self, "_is_optional", self.default is not inspect.Parameter.empty or is_optional(self.annotation)
            )
        return self._is_optional  # type: ignore

    @property
    def is_required(self) -> bool:
        """Check if parameter is required."""
        return not self.is_optional

    @property
    def is_simple_type(self) -> bool:
        """Check if parameter type is simple (str, int, etc.)."""
        if self._is_simple is None:
            unwrapped = self.unwrapped_annotation
            object.__setattr__(self, "_is_simple", is_simple_type(unwrapped))
        return self._is_simple  # type: ignore

    @property
    def is_msgspec_struct(self) -> bool:
        """Check if parameter type is a msgspec.Struct."""
        if self._is_struct is None:
            unwrapped = self.unwrapped_annotation
            object.__setattr__(self, "_is_struct", is_msgspec_struct(unwrapped))
        return self._is_struct  # type: ignore

    @property
    def unwrapped_annotation(self) -> Any:
        """Get annotation with Optional unwrapped."""
        if self._unwrapped is None:
            object.__setattr__(self, "_unwrapped", unwrap_optional(self.annotation))
        return self._unwrapped

    @property
    def origin(self) -> Any:
        """Get the origin type (list, dict, etc.) of the unwrapped annotation."""
        if self._origin is None:
            object.__setattr__(self, "_origin", get_origin(self.unwrapped_annotation))
        return self._origin

    @property
    def field_alias(self) -> str:
        """Get the alias or name."""
        return self.alias or self.name

    @classmethod
    def from_parameter(
        cls,
        parameter: inspect.Parameter,
        annotation: Any,
        path_params: set[str],
        http_method: str,
        explicit_marker: Any = None,
    ) -> FieldDefinition:
        """
        Create FieldDefinition from inspect.Parameter.

        Args:
            parameter: The inspect.Parameter object
            annotation: Type annotation from type hints
            path_params: Set of path parameter names
            http_method: HTTP method for inference
            explicit_marker: Explicit Param or Depends marker if present

        Returns:
            FieldDefinition instance
        """
        name = parameter.name
        default = parameter.default

        # Handle explicit markers
        source: str
        alias: str | None = None
        embed: bool | None = None
        dependency: Any = None
        param: Any = None

        if isinstance(explicit_marker, Param):
            source = explicit_marker.source
            alias = explicit_marker.alias
            embed = explicit_marker.embed
            param = explicit_marker  # Store for file constraints, etc.
        elif isinstance(explicit_marker, DependsMarker):
            source = "dependency"
            dependency = explicit_marker
        else:
            # Infer source from type and context
            source = infer_param_source(name, annotation, path_params, http_method)

        return cls(
            name=name,
            annotation=annotation,
            default=default,
            source=source,
            alias=alias,
            embed=embed,
            dependency=dependency,
            kind=parameter.kind,
            param=param,
        )
