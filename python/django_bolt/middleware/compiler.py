"""Middleware compilation utilities."""

from __future__ import annotations

import datetime
import decimal
import inspect
import ipaddress
import uuid
from collections.abc import Callable
from typing import Annotated, Any, get_args, get_origin

import msgspec
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from ..auth.backends import get_default_authentication_classes
from ..auth.guards import BasePermission, get_default_permission_classes
from ..typing import is_msgspec_struct, unwrap_optional

# Type hint constants - MUST match src/type_coercion.rs
TYPE_INT = 1
TYPE_FLOAT = 2
TYPE_BOOL = 3
TYPE_STRING = 4
TYPE_UUID = 5
TYPE_DATETIME = 6
TYPE_DECIMAL = 7
TYPE_DATE = 8
TYPE_TIME = 9


def get_type_hint_id(annotation: Any) -> int:
    """
    Map Python type annotations to Rust type hint IDs.

    These IDs are used by Rust's type_coercion module to convert
    string parameters to typed values before passing to Python.

    Args:
        annotation: Python type annotation (e.g., int, str, uuid.UUID)

    Returns:
        Type hint ID constant (TYPE_INT, TYPE_STRING, etc.)
    """
    # Unwrap Optional[T] or T | None
    unwrapped = unwrap_optional(annotation)

    # Get base type if it's a generic
    origin = get_origin(unwrapped)

    # Handle Annotated[T, ...] - extract the base type T
    if origin is Annotated:
        args = get_args(unwrapped)
        if args:
            # First arg is the actual type, rest are metadata
            unwrapped = args[0]
            origin = get_origin(unwrapped)

    if origin is not None:
        # For generic types like list[int], we can't coerce in Rust
        return TYPE_STRING

    # Direct type mapping
    if unwrapped is int:
        return TYPE_INT
    elif unwrapped is float:
        return TYPE_FLOAT
    elif unwrapped is bool:
        return TYPE_BOOL
    elif unwrapped is str:
        return TYPE_STRING
    elif unwrapped is uuid.UUID:
        return TYPE_UUID
    elif unwrapped is datetime.datetime:
        return TYPE_DATETIME
    elif unwrapped is datetime.date:
        return TYPE_DATE
    elif unwrapped is datetime.time:
        return TYPE_TIME
    elif unwrapped is decimal.Decimal:
        return TYPE_DECIMAL
    else:
        # Complex types (structs, dicts, etc.) - keep as string
        return TYPE_STRING


def _compile_guard(guard: Any, method: str, path: str) -> dict[str, Any]:
    """Compile one guard (instance or class) to Rust metadata.

    Guards may be passed as instances (`IsAuthenticated()`) or as bare classes
    (`IsAuthenticated`). Anything that cannot be compiled raises: a guard that
    silently fails to register would leave the route unprotected while looking
    protected in the source.
    """
    instance = guard
    if isinstance(guard, type):
        try:
            instance = guard()
        except TypeError as e:
            raise ImproperlyConfigured(
                f"Guard {guard.__name__} on {method} {path} could not be instantiated: {e}. "
                f"Guards that take arguments must be passed as instances, "
                f"e.g. guards=[{guard.__name__}(...)]."
            ) from e

    if not isinstance(instance, BasePermission):
        raise ImproperlyConfigured(
            f"Guard {type(instance).__name__} on {method} {path} is not a guard. "
            f"Guards must subclass django_bolt.auth.BasePermission — use AllowAny, "
            f"IsAuthenticated, or Requires."
        )

    return instance.to_metadata()


def compile_middleware_meta(
    handler: Callable,
    method: str,
    path: str,
    global_middleware: list[Any],
    guards: list[Any] | None = None,
    auth: list[Any] | None = None,
) -> dict[str, Any] | None:
    """Compile middleware metadata for a handler, including guards and auth."""
    # Check for handler-specific middleware
    handler_middleware = []
    skip_middleware: set[str] = set()

    if hasattr(handler, "__bolt_middleware__"):
        handler_middleware = handler.__bolt_middleware__

    if hasattr(handler, "__bolt_skip_middleware__"):
        skip_middleware = handler.__bolt_skip_middleware__

    # Merge global and handler middleware
    all_middleware = []

    # Add global middleware first
    for mw in global_middleware:
        mw_dict = middleware_to_dict(mw)
        if mw_dict and mw_dict.get("type") not in skip_middleware:
            all_middleware.append(mw_dict)

    # Add handler-specific middleware (also filtered by skip_middleware)
    for mw in handler_middleware:
        mw_dict = middleware_to_dict(mw)
        if mw_dict and mw_dict.get("type") not in skip_middleware:
            all_middleware.append(mw_dict)

    # Compile authentication backends
    auth_backends = []
    if auth is not None:
        # Per-route auth override
        for auth_backend in auth:
            if hasattr(auth_backend, "to_metadata"):
                auth_backends.append(auth_backend.to_metadata())
    else:
        # Use global default authentication classes
        for auth_backend in get_default_authentication_classes():
            if hasattr(auth_backend, "to_metadata"):
                auth_backends.append(auth_backend.to_metadata())

    # Compile guards/permissions
    guard_list = []
    if guards is not None:
        # Per-route guards override
        for guard in guards:
            guard_list.append(_compile_guard(guard, method, path))
    else:
        # Use global default permission classes
        for guard in get_default_permission_classes():
            guard_list.append(_compile_guard(guard, method, path))

    # Only include metadata if something is configured
    # Note: include result even when only skip flags are present so Rust can
    #       honor route-level skips like `compression`.
    if not all_middleware and not auth_backends and not guard_list and not skip_middleware:
        return None

    result = {"method": method, "path": path}

    if all_middleware:
        result["middleware"] = all_middleware

        # Rust needs the trusted proxy list to resolve the client address for a
        # `key="ip"` rate limit. Proxies belong to the deployment, not to one
        # route, so resolve the setting once here at registration.
        if any(mw.get("type") == "rate_limit" for mw in all_middleware):
            result["trusted_proxies"] = get_trusted_proxies()

    # Always include skip flags if present (even without middleware/auth/guards)
    if skip_middleware:
        result["skip"] = list(skip_middleware)

    if auth_backends:
        result["auth_backends"] = auth_backends

    if guard_list:
        result["guards"] = guard_list

    return result


def _extract_type_hints_from_field(field: Any, target: dict[str, int], skip_string: bool = False) -> None:
    """Extract type hints from a field (struct or individual) into target dict.

    For struct fields, registers both the attribute name and the encoded name
    (for msgspec field aliases and rename strategies).
    """
    unwrapped = unwrap_optional(field.annotation)
    if is_msgspec_struct(unwrapped):
        for struct_field in msgspec.structs.fields(unwrapped):
            struct_type_hint = get_type_hint_id(struct_field.type)
            if skip_string and struct_type_hint == TYPE_STRING:
                continue
            target[struct_field.name] = struct_type_hint
            encoded_name = getattr(struct_field, "encode_name", struct_field.name)
            if encoded_name != struct_field.name:
                target[encoded_name] = struct_type_hint
    else:
        type_hint = get_type_hint_id(field.annotation)
        if skip_string and type_hint == TYPE_STRING:
            return
        target[field.name] = type_hint


_SEQUENCE_ORIGINS_FOR_FORM = (list, set, frozenset, tuple)


def _collect_form_seq_field_names(field: Any, target: set[str]) -> None:
    """Collect wire-side field names whose declared type is a sequence (list/set/tuple/frozenset).

    Rust uses this set to always emit a Python list for those keys, even when the
    form contained only a single occurrence — eliminating a scalar→list wrap step
    on the Python hot path.
    """
    unwrapped = unwrap_optional(field.annotation)
    if is_msgspec_struct(unwrapped):
        for struct_field in msgspec.structs.fields(unwrapped):
            inner = unwrap_optional(struct_field.type)
            if get_origin(inner) in _SEQUENCE_ORIGINS_FOR_FORM:
                target.add(struct_field.name)
                encoded_name = getattr(struct_field, "encode_name", struct_field.name)
                if encoded_name != struct_field.name:
                    target.add(encoded_name)
    else:
        inner = unwrap_optional(field.annotation)
        if get_origin(inner) in _SEQUENCE_ORIGINS_FOR_FORM:
            target.add(field.alias or field.name)


def _compile_rust_arg_bindings(handler_meta: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Build a Rust-side argument binding plan for simple non-body handlers.

    The plan is used by Rust to pre-bind handler args/kwargs from request maps,
    allowing Python dispatch to skip injector execution on the no-middleware fast path.

    Supports required AND optional scalar params. Optional params are always
    bound as keywords so a missing value can either be omitted (the handler's
    own default applies) or injected as None (Optional[T] annotation with no
    default). When any optional param exists, ALL params are bound as keywords —
    mixing a skipped keyword with positional args would shift positions.
    """
    fields = handler_meta.get("fields", [])
    if not fields:
        return None

    mode = handler_meta.get("mode")
    if mode == "request_only":
        return None

    # Validate every field first; bail out entirely on anything unsupported so
    # the Python injector keeps full ownership of the route's semantics.
    for field in fields:
        if field.source not in ("path", "query", "header", "cookie"):
            return None
        if not field.is_simple_type:
            return None
        if field.origin is not None:
            return None
        if field.kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return None
        # Positional-only optional params can't be keyword-bound → unsupported.
        if field.is_optional and field.kind is inspect.Parameter.POSITIONAL_ONLY:
            return None

    has_optional = any(field.is_optional for field in fields)
    if has_optional and any(field.kind is inspect.Parameter.POSITIONAL_ONLY for field in fields):
        # All-keyword calling convention required, but positional-only params
        # can't participate — fall back to the injector.
        return None

    bindings: list[dict[str, Any]] = []

    for field in fields:
        arg_kind = "keyword" if has_optional or field.kind is inspect.Parameter.KEYWORD_ONLY else "positional"

        if field.source == "header":
            lookup_key = (field.alias or field.name).lower().replace("_", "-")
        else:
            lookup_key = field.alias or field.name

        # inject_none: Optional[T] annotation with no signature default — the
        # extractor semantics yield None for a missing value, so Rust must pass
        # None explicitly (omitting the kwarg would raise a TypeError).
        inject_none = field.is_optional and field.default is inspect.Parameter.empty

        bindings.append(
            {
                "source": field.source,
                "lookup_key": lookup_key,
                "arg_name": field.name,
                "arg_kind": arg_kind,
                "required": not field.is_optional,
                "inject_none": inject_none,
            }
        )

    return bindings if bindings else None


def add_optimization_flags_to_metadata(metadata: dict[str, Any] | None, handler_meta: dict[str, Any]) -> dict[str, Any]:
    """
    Add optimization flags to middleware metadata.

    These flags indicate which request components the handler actually needs,
    allowing Rust to skip parsing unused data.

    Also extracts type hints for path and query parameters to enable
    Rust-side type coercion (avoiding Python's convert_primitive overhead).

    Args:
        metadata: Existing middleware metadata dict (or None to create new)
        handler_meta: Handler metadata containing the optimization flags

    Returns:
        Updated metadata dict with optimization flags and param_types
    """
    if metadata is None:
        metadata = {}

    # Copy optimization flags from handler metadata to middleware metadata
    # These will be parsed by Rust's RouteMetadata::from_python()
    metadata["needs_body"] = handler_meta.get("needs_body", True)
    metadata["needs_query"] = handler_meta.get("needs_query", True)
    metadata["needs_headers"] = handler_meta.get("needs_headers", True)
    metadata["needs_cookies"] = handler_meta.get("needs_cookies", True)
    metadata["needs_path_params"] = handler_meta.get("needs_path_params", True)
    metadata["is_static_route"] = handler_meta.get("is_static_route", False)
    metadata["needs_form_parsing"] = handler_meta.get("needs_form_parsing", False)
    # Default success status for the bare-bytes response fast path: sync
    # executors may return just the encoded JSON body and Rust rebuilds the
    # (status, JSON meta) envelope from this value.
    metadata["default_status_code"] = handler_meta.get("default_status_code", 200)
    # Compile a Rust-side argument binding plan for simple handlers.
    # Rust uses this to pre-bind args/kwargs so Python can skip injector work.
    rust_arg_bindings = _compile_rust_arg_bindings(handler_meta)
    if rust_arg_bindings:
        metadata["rust_arg_bindings"] = rust_arg_bindings

    # Extract type hints for all parameter sources
    # This enables Rust-side type coercion, eliminating Python overhead
    # Format: {"param_name": type_hint_id, ...}
    param_types: dict[str, int] = {}
    form_type_hints: dict[str, int] = {}
    form_seq_fields: set[str] = set()
    file_constraints: dict[str, dict[str, Any]] = {}

    fields = handler_meta.get("fields", [])
    for field in fields:
        # Include type hints for path, query, header, cookie
        if field.source in ("path", "query", "header", "cookie"):
            _extract_type_hints_from_field(field, param_types, skip_string=True)

        # Form fields - extract type hints for Rust-side form parsing
        elif field.source == "form":
            _extract_type_hints_from_field(field, form_type_hints, skip_string=False)
            _collect_form_seq_field_names(field, form_seq_fields)

        # File fields - extract constraints for Rust-side validation
        elif field.source == "file":
            constraints = {}
            if field.param is not None:
                # Extract constraints from ParamMetadata
                if hasattr(field.param, "max_size") and field.param.max_size is not None:
                    constraints["max_size"] = field.param.max_size
                if hasattr(field.param, "min_size") and field.param.min_size is not None:
                    constraints["min_size"] = field.param.min_size
                if hasattr(field.param, "allowed_types") and field.param.allowed_types is not None:
                    constraints["allowed_types"] = list(field.param.allowed_types)
                if hasattr(field.param, "max_files") and field.param.max_files is not None:
                    constraints["max_files"] = field.param.max_files
            if constraints:
                file_constraints[field.name] = constraints

    if param_types:
        metadata["param_types"] = param_types

    if form_type_hints:
        metadata["form_type_hints"] = form_type_hints

    if form_seq_fields:
        metadata["form_seq_fields"] = sorted(form_seq_fields)

    if file_constraints:
        metadata["file_constraints"] = file_constraints

    # Max upload size priority:
    # 1. Per-field max_size (route level) - highest priority
    # 2. BOLT_MAX_UPLOAD_SIZE (Django settings) - global fallback
    # 3. 1MB default
    if file_constraints:
        # Use largest per-field max_size if any field has it
        max_sizes = [c.get("max_size") for c in file_constraints.values() if c.get("max_size")]
        if max_sizes:
            metadata["max_upload_size"] = max(max_sizes)
        else:
            # No per-field max_size, use global setting or default
            metadata["max_upload_size"] = getattr(settings, "BOLT_MAX_UPLOAD_SIZE", 1024 * 1024)
    else:
        # No file constraints at all, use global setting or default
        metadata["max_upload_size"] = getattr(settings, "BOLT_MAX_UPLOAD_SIZE", 1024 * 1024)

    # Memory spool threshold - when to spool files to disk (default 1MB)
    metadata["memory_spool_threshold"] = getattr(settings, "BOLT_MEMORY_SPOOL_THRESHOLD", 1024 * 1024)

    return metadata


def get_trusted_proxies() -> list[str]:
    """
    Read and validate `settings.BOLT_TRUSTED_PROXIES`.

    The setting holds the proxies that sit in front of Bolt, as addresses or
    CIDR blocks. A `key="ip"` rate limit believes `X-Forwarded-For` only from a
    peer in this list. The list is empty by default, so Bolt keys on the peer
    address and ignores forwarding headers.

    Returns:
        Normalized CIDR strings, for example `["10.0.0.0/8", "127.0.0.1/32"]`.

    Raises:
        ImproperlyConfigured: An entry is not an address or a CIDR block.
    """
    configured = getattr(settings, "BOLT_TRUSTED_PROXIES", None)
    # Check the raw value. Normalizing first would turn an empty string into an
    # empty list, and a typo that empties the setting must not read as "no
    # proxies declared".
    if isinstance(configured, str):
        raise ImproperlyConfigured(
            f"BOLT_TRUSTED_PROXIES must be a list of addresses or CIDR blocks, got the string {configured!r}."
        )
    if not configured:
        return []

    normalized = []
    for entry in configured:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except (TypeError, ValueError) as e:
            raise ImproperlyConfigured(
                f"BOLT_TRUSTED_PROXIES entry {entry!r} is not an address or CIDR block: {e}"
            ) from e
        normalized.append(str(network))
    return normalized


def middleware_to_dict(mw: Any) -> dict[str, Any] | None:
    """
    Convert middleware specification to dictionary for Rust metadata.

    Only dict-based middleware configs (from @cors, @rate_limit decorators)
    need to be converted. Python middleware classes/instances are handled
    entirely in Python and don't need serialization to Rust.

    Args:
        mw: Middleware specification (dict from decorators, or Python class/instance)

    Returns:
        Dict if it's a Rust-handled middleware type (cors, rate_limit), None otherwise
    """
    if isinstance(mw, dict):
        # Dict-based config from decorators like @cors() or @rate_limit()
        # These are the only ones Rust needs to know about
        return mw

    # Python middleware classes/instances are handled in Python
    # They don't need to be serialized to Rust metadata
    return None
