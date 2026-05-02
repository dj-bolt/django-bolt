from __future__ import annotations

import asyncio
import contextlib
import dis
import inspect
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from typing import Any, get_origin, get_type_hints

# Django import - may fail if Django not configured
try:
    from django.conf import settings as django_settings
except ImportError:
    django_settings = None


# Import local modules
from django.core.asgi import get_asgi_application
from django.core.signals import request_finished, request_started
from django.db.models import QuerySet
from django.utils.functional import SimpleLazyObject

from . import _json
from ._kwargs import (
    compile_argument_injector,
    compile_binder,
    compile_websocket_binder,
    extract_response_metadata,
)
from ._view_context import _current_action, _current_request
from .admin.routes import AdminRouteRegistrar
from .admin.static_routes import StaticRouteRegistrar
from .analysis import analyze_dependency_tree, analyze_handler, warn_blocking_handler
from .auth import get_default_authentication_classes, register_auth_backend
from .auth.user_loader import load_user_sync
from .concurrency import sync_to_thread
from .decorators import _RESPONSE_MODEL_UNSET, ActionHandler
from .error_handlers import handle_exception
from .exceptions import HTTPException
from .logging.middleware import LoggingMiddleware, create_logging_middleware
from .middleware import CompressionConfig
from .middleware.compiler import _compile_rust_arg_bindings, add_optimization_flags_to_metadata, compile_middleware_meta
from .middleware.django_loader import load_django_middleware
from .middleware.middleware import FunctionMiddlewareSpec, normalize_middleware_specs
from .middleware_response import MiddlewareResponse
from .openapi import (
    OpenAPIConfig,
    RapidocRenderPlugin,
    RedocRenderPlugin,
    ScalarRenderPlugin,
    StoplightRenderPlugin,
    SwaggerRenderPlugin,
)
from .openapi.routes import OpenAPIRouteRegistrar
from .openapi.schema_generator import SchemaGenerator
from .pagination import extract_pagination_item_type, paginate
from .responses import JSON as _JSONResponse
from .router import Router
from .serialization import (
    _BODY_BYTES,
    _RESPONSE_META_EMPTY,
    _RESPONSE_META_JSON,
    ResponseWireV1,
    _convert_serializers,
    _extract_stream_item_type,
    _infer_wire_response_type,
    _wire_bytes,
    compile_response_handlers,
    serialize_json_data,
    serialize_json_data_sync,
    serialize_response,
    serialize_response_sync,
)
from .status_codes import HTTP_201_CREATED, HTTP_204_NO_CONTENT
from .typing import HandlerMetadata, HandlerPattern
from .views import APIView, ViewSet
from .websocket import mark_websocket_handler

Response = ResponseWireV1


# Global registry for BoltAPI instances (used by autodiscovery)
_BOLT_API_REGISTRY = []


def _normalize_path(path: str, trailing_slash: str = "strip") -> str:
    """Normalize a path based on trailing slash mode.

    Args:
        path: The path to normalize
        trailing_slash: Mode for handling trailing slashes:
            - "strip": Remove trailing slashes (default)
            - "append": Add trailing slashes
            - "keep": No normalization

    Returns:
        Normalized path based on the trailing_slash mode
    """
    # Root path "/" is always kept as-is
    if not path or path == "/":
        return "/"

    if trailing_slash == "strip":
        return path.rstrip("/")
    elif trailing_slash == "append":
        return path if path.endswith("/") else path + "/"
    else:  # "keep"
        return path


def _normalize_mount_prefix(path: str) -> str:
    """Normalize an ASGI mount prefix: ensure leading slash, strip trailing, reject dynamic segments."""
    mount_path = "/" + path.strip("/") if path else "/"
    if mount_path != "/" and mount_path.endswith("/"):
        mount_path = mount_path.rstrip("/")

    if "{" in mount_path or "}" in mount_path:
        raise ValueError(f"ASGI mount path must be static (no dynamic parameters), got: {mount_path}")

    return mount_path


def _validate_asgi_mount_conflicts(
    routes: list[tuple[str, str, int, Any]],
    asgi_mounts: list[tuple[str, Any]],
    *,
    error_cls: type[Exception] = ValueError,
) -> None:
    """Reject duplicate ASGI mount prefixes and exact collisions with HTTP routes."""
    if not asgi_mounts:
        return

    route_paths = {path for _method, path, _handler_id, _handler in routes}
    seen_mounts: set[str] = set()

    for mount_prefix, _mount_app in asgi_mounts:
        if mount_prefix in seen_mounts:
            raise error_cls(f"Duplicate ASGI mount prefix: {mount_prefix}")
        seen_mounts.add(mount_prefix)

        if mount_prefix in route_paths:
            raise error_cls(
                f"ASGI mount prefix {mount_prefix} conflicts with an existing HTTP route "
                "(exact collision is not allowed)."
            )


def _rewrite_scope_for_django_mount(scope: dict[str, Any]) -> dict[str, Any]:
    """Prepend root_path to scope["path"] so Django derives path_info correctly.

    Rust `build_scope()` intentionally sets `scope["path"]` to the subpath relative
    to the mount (ASGI convention). Django's ASGI handler expects the full request
    path for URL resolution in mounted setups, so we reconstruct it here.
    """
    if scope.get("type") != "http":
        return scope

    root_path = scope.get("root_path") or ""
    if not root_path:
        return scope

    path = scope.get("path") or "/"
    if not isinstance(path, str):
        path = str(path)
    if not path.startswith("/"):
        path = "/" + path

    if path.startswith(root_path):
        return scope

    full_path = f"{root_path}{path}"

    raw_path_obj = scope.get("raw_path")
    raw_path: bytes
    if isinstance(raw_path_obj, memoryview):
        raw_path = raw_path_obj.tobytes()
    elif isinstance(raw_path_obj, (bytes, bytearray)):
        raw_path = bytes(raw_path_obj)
    elif isinstance(raw_path_obj, str):
        raw_path = raw_path_obj.encode("utf-8")
    else:
        raw_path = path.encode("utf-8")

    root_path_bytes = root_path.encode("utf-8")
    if raw_path.startswith(root_path_bytes):
        full_raw_path = raw_path
    elif raw_path.startswith(b"/"):
        full_raw_path = root_path_bytes + raw_path
    else:
        full_raw_path = full_path.encode("utf-8")

    new_scope = dict(scope)
    new_scope["path"] = full_path
    new_scope["raw_path"] = full_raw_path
    return new_scope


def _rewrite_django_mount_redirect_message(message: dict[str, Any], root_path: str) -> dict[str, Any]:
    """Prepend root_path to root-relative redirect Location headers."""
    if not root_path:
        return message

    if message.get("type") != "http.response.start":
        return message

    status = message.get("status")
    if status not in (301, 302, 303, 307, 308):
        return message

    headers = message.get("headers")
    if not isinstance(headers, (list, tuple)):
        return message

    changed = False
    rewritten: list[tuple[Any, Any]] = []

    for header_name, header_value in headers:
        name_bytes = (
            header_name.tobytes()
            if isinstance(header_name, memoryview)
            else bytes(header_name)
            if isinstance(header_name, (bytes, bytearray))
            else str(header_name).encode("latin1")
        )

        if name_bytes.lower() != b"location":
            rewritten.append((header_name, header_value))
            continue

        value_bytes = (
            header_value.tobytes()
            if isinstance(header_value, memoryview)
            else bytes(header_value)
            if isinstance(header_value, (bytes, bytearray))
            else str(header_value).encode("latin1")
        )
        location = value_bytes.decode("latin1")

        if (
            location.startswith("/")
            and not location.startswith("//")
            and not (location == root_path or location.startswith(root_path + "/"))
        ):
            location = f"{root_path}{location}"
            changed = True
            if isinstance(header_value, (memoryview, bytes, bytearray)):
                rewritten.append((header_name, location.encode("latin1")))
            else:
                rewritten.append((header_name, location))
            continue

        rewritten.append((header_name, header_value))

    if not changed:
        return message

    new_message = dict(message)
    new_message["headers"] = rewritten
    return new_message


def _wire_from_error_parts(status: int, headers: list[tuple[str, str]], body: bytes) -> ResponseWireV1:
    """Convert error-handler parts into ResponseWireV1."""

    custom_content_type = None
    custom_headers: list[tuple[str, str]] = []

    for key, value in headers:
        if key.lower() == "content-type":
            custom_content_type = value
        else:
            custom_headers.append((key, value))

    meta = (
        _infer_wire_response_type(custom_content_type) if custom_content_type else "octetstream",
        custom_content_type,
        custom_headers or None,
        None,
    )
    return int(status), meta, _BODY_BYTES, body


async def serve_with_lifespan(
    source_lifespans: list[tuple[BoltAPI, Callable]],
    server_fn: Callable,
) -> None:
    """Run *server_fn* inside every lifespan context in *source_lifespans*.

    Each ``(api, lifespan_ctx)`` pair is entered in order; teardown runs in
    reverse order (LIFO) via :class:`contextlib.AsyncExitStack`.
    *server_fn* is executed in a thread so the async event-loop stays free
    for lifespan teardown when the server stops.
    """
    async with contextlib.AsyncExitStack() as stack:
        for api, lifespan_ctx in source_lifespans:
            await stack.enter_async_context(lifespan_ctx(api))
        await asyncio.to_thread(server_fn)


class BoltAPI:
    def __init__(
        self,
        prefix: str = "",
        trailing_slash: str = "strip",
        middleware: list[Any] | None = None,
        django_middleware: bool | list[str] | dict[str, Any] | None = None,
        enable_logging: bool = True,
        logging_config: Any | None = None,
        compression: Any | None = None,
        openapi_config: Any | None = None,
        validate_response: bool = True,
        lifespan: Callable | None = None,
    ) -> None:
        """
        Initialize a BoltAPI instance.

        Args:
            prefix: URL prefix for all routes (e.g., "/api/v1")
            trailing_slash: How to handle trailing slashes in routes:
                - "strip": Remove trailing slashes (default, cleaner URLs)
                - "append": Add trailing slashes (Django convention)
                - "keep": No normalization, keep as registered
            middleware: List of Bolt middleware classes (Django-style) or
                DjangoMiddleware/DjangoMiddlewareStack wrappers
            django_middleware: Django middleware configuration. Can be:
                - True: Use all middleware from settings.MIDDLEWARE (excluding CSRF, etc.)
                - False/None: Don't use Django middleware
                - List[str]: Use only these specific Django middleware
                - Dict with "include"/"exclude" keys for fine control
            enable_logging: Enable request/response logging
            logging_config: Custom logging configuration
            compression: Compression configuration (CompressionConfig or False to disable)
            openapi_config: OpenAPI documentation configuration
            validate_response: Default response validation policy for registered routes
            lifespan: Async context manager factory for startup/shutdown lifecycle.
                Receives the BoltAPI instance as argument.
        """
        self._routes: list[tuple[str, str, int, Callable]] = []
        self._websocket_routes: list[tuple[str, int, Callable]] = []  # (path, handler_id, handler)
        self._handlers: dict[int, Callable] = {}
        # OPTIMIZATION: Use handler_id (int) as key instead of callable
        # Integer hashing is O(1) with minimal overhead vs callable hashing
        self._handler_meta: dict[int, HandlerMetadata] = {}
        self._handler_middleware: dict[int, dict[str, Any]] = {}  # Middleware metadata per handler
        self._next_handler_id = 0
        self.prefix = prefix.rstrip("/")  # Remove trailing slash from prefix
        self.trailing_slash = trailing_slash  # Mode: "strip", "append", or "keep"
        self._validate_response_default = validate_response

        # Build middleware list: Django middleware first, then custom middleware
        self._middleware = []

        # Load Django middleware if configured
        # Store flag for optimization bypass (Django middleware needs cookies/headers)
        self._has_django_middleware = bool(django_middleware)
        if django_middleware:
            self._middleware.extend(load_django_middleware(django_middleware))

        # Add custom middleware
        if middleware:
            self._middleware.extend(normalize_middleware_specs(middleware, context="api"))
        self._has_python_global_middleware = any(self._is_python_middleware_spec(spec) for spec in self._middleware)

        # Logging configuration (opt-in, setup happens at server startup)
        self._enable_logging = enable_logging
        self._logging_middleware = None

        if self._enable_logging:
            # Create logging middleware (actual logging setup happens at server startup)
            if logging_config is not None:
                self._logging_middleware = LoggingMiddleware(logging_config)
            else:
                # Use default logging configuration
                self._logging_middleware = create_logging_middleware()

        # Compression configuration
        # compression=None means disabled, not providing compression arg means default enabled
        if compression is False:
            # Explicitly disabled
            self._compression = None
        elif compression is None:
            # Not provided, use default
            self._compression = CompressionConfig()
        else:
            # Custom config provided
            self._compression = compression

        # OpenAPI configuration - enabled by default with sensible defaults
        if openapi_config is None:
            # Create default OpenAPI config
            try:
                # Try to get Django project name from settings
                title = (
                    getattr(django_settings, "PROJECT_NAME", None)
                    or getattr(django_settings, "SITE_NAME", None)
                    or "API"
                    if django_settings
                    else "API"
                )
            except Exception:
                title = "API"

            self._openapi_config = OpenAPIConfig(
                title=title,
                version="1.0.0",
                path="/docs",
                render_plugins=[
                    SwaggerRenderPlugin(path="/"),
                    RedocRenderPlugin(path="/redoc"),
                    ScalarRenderPlugin(path="/scalar"),
                    RapidocRenderPlugin(path="/rapidoc"),
                    StoplightRenderPlugin(path="/stoplight"),
                ],
            )
        else:
            self._openapi_config = openapi_config

        self._openapi_schema: dict[str, Any] | None = None
        self._openapi_routes_registered = False

        # Django admin configuration (controlled by --no-admin flag)
        self._admin_routes_registered = False
        self._static_routes_registered = False
        self._asgi_mounts: list[tuple[str, Callable[..., Any]]] = []
        self._asgi_mount_prefixes: set[str] = set()

        # Middleware chain (built lazily on first request)
        self._middleware_chain_built = False
        self._middleware_chain = None  # Will be the outermost middleware instance
        self._middleware_chain_lock = threading.Lock()
        self._route_executor_cache: dict[Callable, Callable] = {}
        self._route_executor_lock = threading.Lock()

        # Handler-to-API mapping for merged APIs (initialized here to avoid hasattr in hot path)
        self._handler_api_map: dict[int, BoltAPI] = {}

        # Signal emission - disabled by default for performance
        # Enable with BOLT_EMIT_SIGNALS = True in Django settings
        self._emit_signals = getattr(django_settings, "BOLT_EMIT_SIGNALS", False) if django_settings else False
        # Lifecycle: async context manager for startup/shutdown
        self._lifespan_context: Callable | None = lifespan
        self._source_lifespans: list[tuple[BoltAPI, Callable]] | None = None

        # Register this instance globally for autodiscovery
        _BOLT_API_REGISTRY.append(self)

        # Signal support: wrap _dispatch when enabled
        # This is done at init time (not per-request) for zero overhead when disabled
        if self._emit_signals:
            _original_dispatch = self._dispatch

            async def _dispatch_with_signals(handler, request, handler_id=None):
                """Dispatch wrapper that emits Django signals."""
                await request_started.asend(
                    sender=BoltAPI,
                    scope={
                        "type": "http",
                        "method": request.get("method", "GET"),
                        "path": request.get("path", "/"),
                        "query_string": request.get("query_string", b""),
                        "headers": request.get("headers", {}),
                    },
                )
                try:
                    return await _original_dispatch(handler, request, handler_id)
                finally:
                    await request_finished.asend(sender=BoltAPI)

            self._dispatch = _dispatch_with_signals

    def get(
        self,
        path: str,
        *,
        response_model: Any = _RESPONSE_MODEL_UNSET,
        status_code: int | None = None,
        validate_response: bool | None = None,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        response_class: type | None = None,
    ):
        return self._route_decorator(
            "GET",
            path,
            response_model=response_model,
            status_code=status_code,
            validate_response=validate_response,
            guards=guards,
            auth=auth,
            tags=tags,
            summary=summary,
            description=description,
            response_class=response_class,
        )

    def post(
        self,
        path: str,
        *,
        response_model: Any = _RESPONSE_MODEL_UNSET,
        status_code: int | None = None,
        validate_response: bool | None = None,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        response_class: type | None = None,
    ):
        return self._route_decorator(
            "POST",
            path,
            response_model=response_model,
            status_code=status_code,
            validate_response=validate_response,
            guards=guards,
            auth=auth,
            tags=tags,
            summary=summary,
            description=description,
            response_class=response_class,
        )

    def put(
        self,
        path: str,
        *,
        response_model: Any = _RESPONSE_MODEL_UNSET,
        status_code: int | None = None,
        validate_response: bool | None = None,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        response_class: type | None = None,
    ):
        return self._route_decorator(
            "PUT",
            path,
            response_model=response_model,
            status_code=status_code,
            validate_response=validate_response,
            guards=guards,
            auth=auth,
            tags=tags,
            summary=summary,
            description=description,
            response_class=response_class,
        )

    def patch(
        self,
        path: str,
        *,
        response_model: Any = _RESPONSE_MODEL_UNSET,
        status_code: int | None = None,
        validate_response: bool | None = None,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        response_class: type | None = None,
    ):
        return self._route_decorator(
            "PATCH",
            path,
            response_model=response_model,
            status_code=status_code,
            validate_response=validate_response,
            guards=guards,
            auth=auth,
            tags=tags,
            summary=summary,
            description=description,
            response_class=response_class,
        )

    def delete(
        self,
        path: str,
        *,
        response_model: Any = _RESPONSE_MODEL_UNSET,
        status_code: int | None = None,
        validate_response: bool | None = None,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        response_class: type | None = None,
    ):
        return self._route_decorator(
            "DELETE",
            path,
            response_model=response_model,
            status_code=status_code,
            validate_response=validate_response,
            guards=guards,
            auth=auth,
            tags=tags,
            summary=summary,
            description=description,
            response_class=response_class,
        )

    def head(
        self,
        path: str,
        *,
        response_model: Any = _RESPONSE_MODEL_UNSET,
        status_code: int | None = None,
        validate_response: bool | None = None,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        response_class: type | None = None,
    ):
        return self._route_decorator(
            "HEAD",
            path,
            response_model=response_model,
            status_code=status_code,
            validate_response=validate_response,
            guards=guards,
            auth=auth,
            tags=tags,
            summary=summary,
            description=description,
            response_class=response_class,
        )

    def options(
        self,
        path: str,
        *,
        response_model: Any = _RESPONSE_MODEL_UNSET,
        status_code: int | None = None,
        validate_response: bool | None = None,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        response_class: type | None = None,
    ):
        return self._route_decorator(
            "OPTIONS",
            path,
            response_model=response_model,
            status_code=status_code,
            validate_response=validate_response,
            guards=guards,
            auth=auth,
            tags=tags,
            summary=summary,
            description=description,
            response_class=response_class,
        )

    def websocket(
        self,
        path: str,
        *,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
    ):
        """
        Register a WebSocket endpoint with FastAPI-like syntax.

        Usage:
            from django_bolt.websocket import WebSocket

            @api.websocket("/ws")
            async def websocket_endpoint(websocket: WebSocket):
                await websocket.accept()
                while True:
                    data = await websocket.receive_text()
                    await websocket.send_text(f"Echo: {data}")

            @api.websocket("/ws/{room_id}")
            async def room_websocket(websocket: WebSocket, room_id: str):
                await websocket.accept()
                await websocket.send_json({"room": room_id})
                async for message in websocket.iter_json():
                    await websocket.send_json({"echo": message})
        """
        return self._websocket_decorator(path, guards=guards, auth=auth)

    def _websocket_decorator(
        self,
        path: str,
        *,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
    ):
        """Internal decorator for WebSocket routes."""

        def decorator(fn: Callable) -> Callable:
            # Ensure handler is async
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(f"WebSocket handler '{fn.__name__}' must be an async function")

            # Mark as WebSocket handler
            fn = mark_websocket_handler(fn)

            # Assign handler ID
            handler_id = self._next_handler_id
            self._next_handler_id += 1

            # Build full path with prefix
            full_path = f"{self.prefix}{path}" if self.prefix else path

            # Store the route
            self._websocket_routes.append((full_path, handler_id, fn))
            self._handlers[handler_id] = fn

            # Compile parameter binder for WebSocket (reuses HTTP binding logic)
            # This enables injection of path params, query params, headers, cookies
            meta = self._compile_websocket_binder(fn, full_path)
            meta["is_async"] = True
            meta["is_websocket"] = True

            # Compile optimized argument injector (same as HTTP handlers)
            injector = self._compile_argument_injector(meta)
            meta["injector"] = injector
            meta["injector_is_async"] = inspect.iscoroutinefunction(injector)

            self._handler_meta[handler_id] = meta

            # Compile middleware metadata for WebSocket handler
            # Always call compile_middleware_meta to pick up:
            # 1. Handler-level decorators (@rate_limit, @cors, etc.)
            # 2. Global middleware from self._middleware
            # 3. Guards and auth backends
            middleware_meta = compile_middleware_meta(
                handler=fn,
                method="WEBSOCKET",
                path=full_path,
                global_middleware=self._middleware,
                guards=guards,
                auth=auth,
            )

            # Add optimization flags and param_types to middleware metadata
            # These enable Rust-side type coercion for path/query params
            middleware_meta = add_optimization_flags_to_metadata(middleware_meta, meta)

            if middleware_meta:
                self._handler_middleware[handler_id] = middleware_meta
                # Store auth backend instances for user resolution
                if auth is not None:
                    middleware_meta["_auth_backend_instances"] = auth

            return fn

        return decorator

    def view(
        self,
        path: str,
        *,
        methods: list[str] | None = None,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
        status_code: int | None = None,
        validate_response: bool | None = None,
        tags: list[str] | None = None,
    ):
        """
        Register a class-based view as a decorator.

        Usage:
            @api.view("/users")
            class UserView(APIView):
                async def get(self) -> list[User]:
                    return User.objects.all()[:10]

        This method discovers available HTTP method handlers from the view class
        and registers them with the router. It supports the same parameter extraction,
        dependency injection, guards, and authentication as function-based handlers.

        Args:
            path: URL path pattern (e.g., "/users/{user_id}")
            methods: Optional list of HTTP methods to register (defaults to all implemented methods)
            guards: Optional per-route guard overrides (merged with class-level guards)
            auth: Optional per-route auth overrides (merged with class-level auth)
            status_code: Optional per-route status code override
            tags: Optional per-route tags override

        Returns:
            Decorator function that registers the view class

        Raises:
            ValueError: If view class doesn't implement any requested methods
        """

        def decorator(view_cls: type) -> type:
            # Validate that view_cls is an APIView subclass
            if not issubclass(view_cls, APIView):
                raise TypeError(f"View class {view_cls.__name__} must inherit from APIView")

            # Determine which methods to register
            if methods is None:
                # Auto-discover all implemented methods
                available_methods = view_cls.get_allowed_methods()
                if not available_methods:
                    raise ValueError(f"View class {view_cls.__name__} does not implement any HTTP methods")
                methods_to_register = [m.lower() for m in available_methods]
            else:
                # Validate requested methods are implemented
                methods_to_register = [m.lower() for m in methods]
                available_methods = {m.lower() for m in view_cls.get_allowed_methods()}
                for method in methods_to_register:
                    if method not in available_methods:
                        raise ValueError(f"View class {view_cls.__name__} does not implement method '{method}'")

            # Register each method
            for method in methods_to_register:
                method_upper = method.upper()

                # Create handler using as_view()
                handler = view_cls.as_view(method)

                # Merge guards: route-level overrides class-level
                merged_guards = guards
                if merged_guards is None and hasattr(handler, "__bolt_guards__"):
                    merged_guards = handler.__bolt_guards__

                # Merge auth: route-level overrides class-level
                merged_auth = auth
                if merged_auth is None and hasattr(handler, "__bolt_auth__"):
                    merged_auth = handler.__bolt_auth__

                # Merge status_code: route-level overrides class-level
                merged_status_code = status_code
                if merged_status_code is None and hasattr(handler, "__bolt_status_code__"):
                    merged_status_code = handler.__bolt_status_code__

                merged_validate_response = validate_response
                if merged_validate_response is None:
                    merged_validate_response = getattr(handler, "validate_response", None)
                if merged_validate_response is None and hasattr(handler, "__bolt_validate_response__"):
                    merged_validate_response = handler.__bolt_validate_response__

                # Register using existing route decorator
                route_decorator = self._route_decorator(
                    method_upper,
                    path,
                    response_model=_RESPONSE_MODEL_UNSET,  # Use method's return annotation
                    status_code=merged_status_code,
                    validate_response=merged_validate_response,
                    guards=merged_guards,
                    auth=merged_auth,
                    tags=tags,
                )

                # Apply decorator to register the handler
                route_decorator(handler)

            # Scan for custom action methods (methods decorated with @action)
            # Note: api.view() doesn't have base path context for @action decorator
            # Custom actions with @action should use api.viewset() instead
            self._register_custom_actions(view_cls, base_path=None, lookup_field=None)

            return view_cls

        return decorator

    def viewset(
        self,
        path: str,
        *,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
        status_code: int | None = None,
        validate_response: bool | None = None,
        lookup_field: str = "pk",
        tags: list[str] | None = None,
    ):
        """
        Register a ViewSet with automatic CRUD route generation as a decorator.

        Usage:
            @api.viewset("/users")
            class UserViewSet(ViewSet):
                async def list(self, request) -> list[User]:
                    return User.objects.all()[:100]

                async def retrieve(self, request, id: int) -> User:
                    return await User.objects.aget(id=id)

                @action(methods=["POST"], detail=True)
                async def activate(self, request, id: int):
                    user = await User.objects.aget(id=id)
                    user.is_active = True
                    await user.asave()
                    return user

        This method auto-generates routes for standard DRF-style actions:
        - list: GET /path (200 OK)
        - create: POST /path (201 Created)
        - retrieve: GET /path/{pk} (200 OK)
        - update: PUT /path/{pk} (200 OK)
        - partial_update: PATCH /path/{pk} (200 OK)
        - destroy: DELETE /path/{pk} (204 No Content)

        Args:
            path: Base URL path (e.g., "/users")
            guards: Optional guards to apply to all routes
            auth: Optional auth backends to apply to all routes
            status_code: Optional default status code (overrides action-specific defaults)
            lookup_field: Field name for object lookup (default: "pk")
            tags: Optional tags to apply to all routes

        Returns:
            Decorator function that registers the viewset
        """

        def decorator(viewset_cls: type[ViewSet]) -> type[ViewSet]:
            # Validate that viewset_cls is a ViewSet subclass
            if not issubclass(viewset_cls, ViewSet):
                raise TypeError(f"ViewSet class {viewset_cls.__name__} must inherit from ViewSet")

            # Use lookup_field from ViewSet class if not provided
            actual_lookup_field = lookup_field
            if actual_lookup_field == "pk" and hasattr(viewset_cls, "lookup_field"):
                actual_lookup_field = viewset_cls.lookup_field

            # Define standard action mappings with HTTP-compliant status codes
            # Format: action_name: (method, path, default_status_code)
            action_routes = {
                # Collection routes (no pk)
                "list": ("GET", path, None),
                "create": ("POST", path, HTTP_201_CREATED),
                # Detail routes (with pk)
                "retrieve": ("GET", f"{path}/{{{actual_lookup_field}}}", None),
                "update": ("PUT", f"{path}/{{{actual_lookup_field}}}", None),
                "partial_update": ("PATCH", f"{path}/{{{actual_lookup_field}}}", None),
                "destroy": ("DELETE", f"{path}/{{{actual_lookup_field}}}", HTTP_204_NO_CONTENT),
            }

            # Register routes for each implemented action
            for action_name, (http_method, route_path, action_status_code) in action_routes.items():
                # Check if the viewset implements this action
                if not hasattr(viewset_cls, action_name):
                    continue

                action_method = getattr(viewset_cls, action_name)
                if not inspect.iscoroutinefunction(action_method):
                    continue

                # Use action name (e.g., "list") not HTTP method name (e.g., "get")
                handler = viewset_cls.as_view(http_method.lower(), action=action_name)

                # Merge guards and auth
                merged_guards = guards
                if merged_guards is None and hasattr(handler, "__bolt_guards__"):
                    merged_guards = handler.__bolt_guards__

                merged_auth = auth
                if merged_auth is None and hasattr(handler, "__bolt_auth__"):
                    merged_auth = handler.__bolt_auth__

                # Status code priority: explicit status_code param > handler attribute > action default
                merged_status_code = status_code
                if merged_status_code is None and hasattr(handler, "__bolt_status_code__"):
                    merged_status_code = handler.__bolt_status_code__
                if merged_status_code is None:
                    merged_status_code = action_status_code

                # Check for response_model on the method or original handler
                method_response_model = getattr(action_method, "response_model", _RESPONSE_MODEL_UNSET)
                if method_response_model is _RESPONSE_MODEL_UNSET:
                    original = getattr(handler, "__original_handler__", None)
                    if original is not None:
                        method_response_model = getattr(original, "response_model", _RESPONSE_MODEL_UNSET)

                # Extract response model from type hints if not already determined
                if method_response_model is _RESPONSE_MODEL_UNSET:
                    globalns = sys.modules.get(handler.__module__, {}).__dict__ if handler.__module__ else {}
                    type_hints = get_type_hints(handler, globalns=globalns, include_extras=True)
                    method_response_model = type_hints.get("return", _RESPONSE_MODEL_UNSET)

                # Fallback to the viewset's serializer class when possible, but
                # don't let dynamic runtime selection crash registration.
                if method_response_model is _RESPONSE_MODEL_UNSET:
                    inferred_serializer = self._infer_viewset_serializer_class(
                        viewset_cls,
                        action_name if action_name == "list" else "retrieve",
                    )
                    if inferred_serializer is not _RESPONSE_MODEL_UNSET:
                        if action_name == "list":
                            method_response_model = list[inferred_serializer]
                        elif action_name != "destroy":
                            method_response_model = inferred_serializer

                merged_validate_response = validate_response
                if merged_validate_response is None:
                    merged_validate_response = getattr(action_method, "validate_response", None)
                if merged_validate_response is None:
                    original = getattr(handler, "__original_handler__", None)
                    if original is not None:
                        merged_validate_response = getattr(original, "validate_response", None)
                if merged_validate_response is None and hasattr(handler, "__bolt_validate_response__"):
                    merged_validate_response = handler.__bolt_validate_response__

                # Apply pagination decorator for list actions when  handler is not paginated and pagination class is configured
                if (
                    action_name == "list"
                    and not getattr(handler, "__paginated__", False)
                    and viewset_cls.pagination_class
                ):
                    handler = paginate(viewset_cls.pagination_class)(handler)

                # Register the route
                route_decorator = self._route_decorator(
                    http_method,
                    route_path,
                    response_model=method_response_model,
                    status_code=merged_status_code,
                    validate_response=merged_validate_response,
                    guards=merged_guards,
                    auth=merged_auth,
                    tags=tags,
                )
                route_decorator(handler)

            # Scan for custom actions (@action decorator)
            self._register_custom_actions(viewset_cls, base_path=path, lookup_field=actual_lookup_field)

            return viewset_cls

        return decorator

    def _infer_viewset_serializer_class(self, viewset_cls: type[ViewSet], action_name: str):
        """
        Best-effort serializer inference for unannotated viewset actions.

        Called at registration time (no request context), so ``self.action``
        is unavailable. We pass *action_name* explicitly instead.
        """
        try:
            view_instance = viewset_cls()
            return view_instance.get_serializer_class(action_name)
        except (AttributeError, TypeError, ValueError):
            return _RESPONSE_MODEL_UNSET

    def _register_custom_actions(self, view_cls: type, base_path: str | None, lookup_field: str | None):
        """
        Scan a ViewSet class for custom action methods and register them.

        Custom actions are methods decorated with @action decorator.

        Args:
            view_cls: The ViewSet class to scan
            base_path: Base path for the ViewSet (e.g., "/users")
            lookup_field: Lookup field name for detail actions (e.g., "id", "pk")
        """

        # Get class-level auth and guards (if any)
        class_auth = getattr(view_cls, "auth", None)
        class_guards = getattr(view_cls, "guards", None)
        class_validate_response = getattr(view_cls, "validate_response", None)

        # Scan all attributes in the class
        for name in dir(view_cls):
            # Skip private attributes and standard action methods
            if name.startswith("_") or name.lower() in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
                "list",
                "retrieve",
                "create",
                "update",
                "partial_update",
                "destroy",
            }:
                continue

            attr = getattr(view_cls, name)

            # Check if it's an ActionHandler instance (decorated with @action)
            if isinstance(attr, ActionHandler):
                # Validate that we have base_path for auto-generation
                if base_path is None:
                    raise ValueError(
                        f"Custom action {view_cls.__name__}.{name} uses @action decorator, "
                        f"but ViewSet was registered with api.view() instead of api.viewset(). "
                        f"Use api.viewset() for automatic action path generation."
                    )

                # Extract the unbound function from the ActionHandler
                unbound_fn = attr.fn

                # Auto-generate route path based on detail flag
                if attr.detail:
                    # Instance-level action: /base_path/{lookup_field}/action_name
                    # Example: /users/{id}/activate
                    action_path = f"{base_path}/{{{lookup_field}}}/{attr.path}"
                else:
                    # Collection-level action: /base_path/action_name
                    # Example: /users/active
                    action_path = f"{base_path}/{attr.path}"

                # Register route for each HTTP method
                for http_method in attr.methods:
                    # Create a wrapper that instantiates the view per request,
                    # sets the ContextVar-based request/action, and forwards.
                    is_async_method = inspect.iscoroutinefunction(unbound_fn)
                    if is_async_method:

                        async def custom_action_handler(
                            *args,
                            __unbound_fn=unbound_fn,
                            __view_cls=view_cls,
                            _action=name,
                            **kwargs,
                        ):
                            """Wrapper for custom action method."""
                            _current_action.set(_action)
                            _current_request.set(args[0])
                            view_instance = __view_cls()
                            return await __unbound_fn(view_instance, *args, **kwargs)
                    else:

                        def custom_action_handler(
                            *args,
                            __unbound_fn=unbound_fn,
                            __view_cls=view_cls,
                            _action=name,
                            **kwargs,
                        ):
                            """Wrapper for custom action method."""
                            _current_action.set(_action)
                            _current_request.set(args[0])
                            view_instance = __view_cls()
                            return __unbound_fn(view_instance, *args, **kwargs)

                    # Preserve signature and annotations from original method
                    sig = inspect.signature(unbound_fn)
                    params = list(sig.parameters.values())[1:]  # Skip 'self'
                    custom_action_handler.__signature__ = sig.replace(parameters=params)
                    custom_action_handler.__annotations__ = {
                        k: v for k, v in inspect.get_annotations(unbound_fn).items() if k != "self"
                    }
                    custom_action_handler.__name__ = f"{view_cls.__name__}.{name}"
                    custom_action_handler.__doc__ = unbound_fn.__doc__
                    custom_action_handler.__module__ = unbound_fn.__module__

                    # Merge class-level auth/guards with action-specific auth/guards
                    # Action-specific takes precedence if explicitly set
                    final_auth = attr.auth if attr.auth is not None else class_auth
                    final_guards = attr.guards if attr.guards is not None else class_guards
                    final_validate_response = (
                        attr.validate_response if attr.validate_response is not None else class_validate_response
                    )

                    # Register the custom action
                    decorator = self._route_decorator(
                        http_method,
                        action_path,
                        response_model=attr.response_model,
                        status_code=attr.status_code,
                        validate_response=final_validate_response,
                        guards=final_guards,
                        auth=final_auth,
                        tags=attr.tags,
                        summary=attr.summary,
                        description=attr.description,
                    )
                    decorator(custom_action_handler)

    def _route_decorator(
        self,
        method: str,
        path: str,
        *,
        response_model: Any = _RESPONSE_MODEL_UNSET,
        status_code: int | None = None,
        validate_response: bool | None = None,
        guards: list[Any] | None = None,
        auth: list[Any] | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        response_class: type | None = None,
        _skip_prefix: bool = False,
        _router_middleware: list[Any] | None = None,
    ):
        def decorator(fn: Callable):
            # Detect if handler is async or sync
            is_async = inspect.iscoroutinefunction(fn)

            handler_id = self._next_handler_id
            self._next_handler_id += 1

            # Normalize path (following Starlette conventions):
            # - Empty "" becomes "/"
            # - Trailing slashes are stripped (except for root "/")
            normalized_path = path if path else "/"

            # Apply prefix to path (conversion happens in Rust)
            # _skip_prefix is used internally for OpenAPI routes that should be at absolute paths
            if _skip_prefix:
                full_path = normalized_path
            elif self.prefix:
                full_path = self.prefix + normalized_path
            else:
                full_path = normalized_path

            # Normalize trailing slash based on setting
            full_path = _normalize_path(full_path, self.trailing_slash)

            self._routes.append((method, full_path, handler_id, fn))
            self._handlers[handler_id] = fn

            # Pre-compile parameter binder (handles parameter binding only)
            meta = self._compile_binder(fn, method, full_path)

            # Store sync/async metadata
            meta["is_async"] = is_async

            # Detect csrf_exempt for Django CSRF middleware support
            # Django's @csrf_exempt decorator sets handler.csrf_exempt = True
            meta["csrf_exempt"] = getattr(fn, "csrf_exempt", False)

            request_param_names = {
                field.name for field in meta.get("fields", []) if getattr(field, "source", None) == "request"
            }

            # Static ORM + request-component analysis at registration time
            handler_analysis = analyze_handler(fn, request_param_names=request_param_names)

            if request_param_names:
                if handler_analysis.analysis_failed:
                    # Preserve correctness when source introspection is unavailable.
                    meta["needs_body"] = True
                    meta["needs_query"] = True
                    meta["needs_headers"] = True
                    meta["needs_cookies"] = True
                else:
                    meta["needs_body"] = meta.get("needs_body", False) or handler_analysis.request_needs_body
                    meta["needs_query"] = meta.get("needs_query", False) or handler_analysis.request_needs_query
                    meta["needs_headers"] = meta.get("needs_headers", False) or handler_analysis.request_needs_headers
                    meta["needs_cookies"] = meta.get("needs_cookies", False) or handler_analysis.request_needs_cookies

            # Recursively analyze Depends targets so a dep reading request.query
            # (etc.) causes the handler's route to actually parse query params.
            # Populate self._handler_meta by callable so runtime dep resolution
            # (dependencies.resolve_dependency) reuses the compiled meta too.
            def _compile_dep(dep_fn: Callable) -> dict[str, Any]:
                cached = self._handler_meta.get(dep_fn)
                if cached is not None:
                    return cached
                compiled = self._compile_binder(dep_fn, method, full_path)
                self._handler_meta[dep_fn] = compiled
                return compiled

            dep_needs = analyze_dependency_tree(meta, _compile_dep)
            for needs_key in ("needs_body", "needs_query", "needs_headers", "needs_cookies"):
                if getattr(dep_needs, needs_key):
                    meta[needs_key] = True

            meta["is_blocking"] = handler_analysis.is_blocking

            # Emit warning for sync handlers with ORM (will run in thread pool)
            warn_blocking_handler(fn, full_path, is_async, handler_analysis)

            # Determine final response type with proper priority:
            # 1. response_model parameter (explicit, takes precedence)
            # 2. sig.return_annotation (fallback if response_model not provided)
            final_response_type = None
            # Initialized here to avoid unbound variable risk in the default_status_code
            # assignment below when is_multi_response=True.
            effective_status: int | None = None
            default_code: int = 0
            route_validate_response = (
                self._validate_response_default if validate_response is None else validate_response
            )
            if response_model is not _RESPONSE_MODEL_UNSET:
                if response_model is None:
                    meta["is_multi_response"] = False
                    final_response_type = None
                elif isinstance(response_model, dict):
                    # Dict mode: per-status-code response schemas
                    int_codes = sorted(c for c in response_model if isinstance(c, int))
                    if not int_codes:
                        raise ValueError(
                            "response_model dict must contain at least one integer "
                            "status code key (e.g. {200: Schema, ...: Fallback})"
                        )

                    meta["is_multi_response"] = True
                    meta["response_map"] = response_model

                    # Pre-compute field names for each code's type
                    field_names_map = {}
                    for code, resp_type in response_model.items():
                        if resp_type is not None and code is not ...:
                            resp_meta = extract_response_metadata(resp_type)
                            if "response_field_names" in resp_meta:
                                field_names_map[code] = resp_meta["response_field_names"]
                    if field_names_map:
                        meta["response_field_names_map"] = field_names_map

                    # Determine effective default status code
                    effective_status = status_code
                    if effective_status is None:
                        success_codes = [c for c in int_codes if 200 <= c < 300]
                        if success_codes:
                            effective_status = success_codes[0]

                    # Set response_type to default code's type (backward compat)
                    default_code = effective_status if effective_status is not None else int_codes[0]
                    default_type = response_model.get(default_code)
                    if default_type is not None:
                        final_response_type = default_type
                else:
                    # Single type (existing behavior, unchanged)
                    meta["is_multi_response"] = False
                    final_response_type = response_model
            else:
                # No response_model - check for return annotation
                meta["is_multi_response"] = False
                # Need to resolve string annotations (from __future__ import annotations)
                globalns = sys.modules.get(fn.__module__, {}).__dict__ if fn.__module__ else {}
                type_hints = get_type_hints(fn, globalns=globalns, include_extras=True)
                final_response_type = type_hints.get("return", None)

            # Extract metadata from final type (after priority resolution)
            if final_response_type is not None:
                meta["response_type"] = final_response_type
                # Pre-compute field names for QuerySet optimization (registration time only)
                response_meta = extract_response_metadata(final_response_type)
                meta.update(response_meta)
            else:
                meta["response_type"] = None
            meta["validate_response"] = route_validate_response
            meta["response_class"] = response_class
            # Pre-compute stream annotation analysis (registration time only)
            meta["_stream_info"] = _extract_stream_item_type(meta["response_type"])

            # If handler is paginated, extract and store the item serializer
            # This enables @paginate to use Serializer.dump_many() for efficient serialization
            if getattr(fn, "__paginated__", False) and final_response_type is not None:
                item_type = extract_pagination_item_type(final_response_type)
                if item_type is not None:
                    fn.__serializer_class__ = item_type
                    # For ViewSet methods wrapped by as_view(), also set on the original handler
                    original = getattr(fn, "__original_handler__", None)
                    if original is not None:
                        original.__serializer_class__ = item_type

            # Guarantee all keys exist at registration time for direct access
            if meta["is_multi_response"]:
                meta["default_status_code"] = int(effective_status) if effective_status is not None else default_code
            else:
                meta["default_status_code"] = int(status_code) if status_code is not None else 200
            # Store OpenAPI metadata
            if tags is not None:
                meta["openapi_tags"] = tags
            if summary is not None:
                meta["openapi_summary"] = summary
            if description is not None:
                meta["openapi_description"] = description

            # Pre-compute per-status-code mini-metas at registration time.
            # Only store the 3-4 keys actually read by the serialization hot path,
            # avoiding a full dict(meta) copy (~25 keys) per status code entry.
            # Keys needed: response_type, default_status_code, is_multi_response,
            # and optionally response_field_names (for QuerySet .values() optimisation).
            if meta["is_multi_response"]:
                resolved_metas: dict[int | type(...), dict] = {}
                field_names_map = meta.get("response_field_names_map", {})
                handler_default_status = meta["default_status_code"]
                for code, resp_type in meta["response_map"].items():
                    entry: dict = {
                        "response_type": resp_type,
                        "default_status_code": code if isinstance(code, int) else handler_default_status,
                        "validate_response": route_validate_response,
                        "_stream_info": _extract_stream_item_type(resp_type),
                    }
                    if code in field_names_map:
                        entry["response_field_names"] = field_names_map[code]
                    resolved_metas[code] = entry
                meta["_resolved_metas"] = resolved_metas

            compile_response_handlers(meta)
            if meta["is_multi_response"]:
                for entry in meta["_resolved_metas"].values():
                    compile_response_handlers(entry)

            # Compile optimized argument injector (once at registration time)
            # This pre-compiles all parameter extraction logic for maximum performance
            injector = self._compile_argument_injector(meta)
            meta["injector"] = injector
            # Store whether injector is async (avoids runtime check with inspect.iscoroutinefunction)
            meta["injector_is_async"] = inspect.iscoroutinefunction(injector)
            meta["_original_fn"] = fn
            meta["_handler_executor"] = self._compile_handler_executor(meta)

            # Normalize route-level middleware declared via @middleware / @cors / @rate_limit.
            # Validation happens at registration time to fail fast and deterministically.
            route_middleware = normalize_middleware_specs(
                getattr(fn, "__bolt_middleware__", []),
                context="route",
                allow_function_middleware=True,
            )
            if route_middleware or hasattr(fn, "__bolt_middleware__"):
                fn.__bolt_middleware__ = route_middleware

            router_middleware = normalize_middleware_specs(_router_middleware, context="router")

            # Preserve normalized middleware layers in handler metadata for runtime execution.
            meta["_router_middleware"] = router_middleware
            meta["_route_middleware"] = route_middleware
            meta["_has_route_python_middleware"] = any(
                not isinstance(spec, dict) for spec in [*router_middleware, *route_middleware]
            )

            self._handler_meta[handler_id] = meta

            # Compile middleware metadata for this handler (including guards and auth)
            middleware_meta = compile_middleware_meta(
                fn,
                method,
                full_path,
                [*self._middleware, *router_middleware],
                guards=guards,
                auth=auth,
            )

            # Add optimization flags to middleware metadata
            # These are parsed by Rust's RouteMetadata::from_python() to skip unused parsing
            middleware_meta = add_optimization_flags_to_metadata(middleware_meta, meta)

            # Resolve effective auth backends once (explicit > defaults) — reused
            # below for revocation precomputation and _auth_backend_instances.
            effective_auth_backends = auth if auth is not None else (get_default_authentication_classes() or [])

            # scheme_name → handler. Lookup at dispatch is O(1) via the
            # matched backend's name. None when no backend has revocation.
            revocation_handlers: dict[str, Callable] = {
                b.scheme_name: b.revoked_token_handler
                for b in effective_auth_backends
                if getattr(b, "revoked_token_handler", None) is not None
            }
            meta["_revocation_handlers"] = revocation_handlers or None

            # Sync dispatch bypass requires no awaits in the request flow.
            # Revocation handlers are awaited, so opt out when configured.
            can_sync_dispatch = (
                "_sync_executor" in meta
                and not meta["_has_route_python_middleware"]
                and not self._has_python_global_middleware
                and not self._has_django_middleware
                and not self._emit_signals
                and not revocation_handlers
            )
            middleware_meta["can_sync_dispatch"] = can_sync_dispatch

            # Python middleware requires cookies and headers regardless of handler params
            # Django middleware needs cookies/headers (CSRF, session, auth, etc.)
            # Custom middleware may also inspect headers for routing, auth, etc.
            if (
                self._has_django_middleware
                or self._has_python_global_middleware
                or meta["_has_route_python_middleware"]
            ):
                middleware_meta["needs_cookies"] = True
                middleware_meta["needs_headers"] = True

            if middleware_meta:
                self._handler_middleware[handler_id] = middleware_meta
                # Backend instances (not metadata) are kept so user resolution
                # can call their get_user() methods.
                if effective_auth_backends:
                    middleware_meta["_auth_backend_instances"] = effective_auth_backends

            return fn

        return decorator

    def _compile_binder(self, fn: Callable, http_method: str = "", path: str = "") -> HandlerMetadata:
        """Delegate to compile_binder in api_compilation module."""
        return compile_binder(fn, http_method, path)

    def _compile_websocket_binder(self, fn: Callable, path: str) -> HandlerMetadata:
        """Delegate to compile_websocket_binder in api_compilation module."""
        return compile_websocket_binder(fn, path)

    def _compile_argument_injector(
        self, meta: HandlerMetadata
    ) -> Callable[[dict[str, Any]], tuple[list[Any], dict[str, Any]]]:
        """Delegate to compile_argument_injector in api_compilation module."""
        return compile_argument_injector(meta, self._handler_meta, self._compile_binder)

    def _compile_handler_executor(self, meta: HandlerMetadata) -> Callable[..., Any]:
        """Compile per-handler execution callable for the request hot path."""
        mode = meta["mode"]
        is_async = meta["is_async"]
        is_blocking = meta.get("is_blocking", False)
        injector = meta["injector"]
        injector_is_async = meta["injector_is_async"]

        # Pre-compute at registration time: can Rust ever pre-bind args into request.state?
        # When False, we skip the state.setdefault + 2 pop calls on every request.
        has_rust_prebound = bool(_compile_rust_arg_bindings(meta))

        # Multi-response: specialize the executor at registration time so that the
        # is_multi branching never touches the hot path of normal (single-schema) routes.
        if meta.get("is_multi_response"):
            resolved_metas = meta["_resolved_metas"]
            default_status = meta["default_status_code"]
            default_entry = resolved_metas.get(default_status) or next(iter(resolved_metas.values()))

            def _lookup(code: int) -> dict:
                entry = resolved_metas.get(code)
                if entry is None:
                    entry = resolved_metas.get(...)  # ellipsis catch-all
                if entry is None:
                    defined = sorted(c for c in resolved_metas if c is not ...)
                    raise TypeError(f"Status {code} has no response schema. Defined: {defined}")
                return entry

            async def _dispatch_multi_async(result: Any) -> ResponseWireV1:
                # Tuple (status, data): the primary multi-response return pattern.
                if isinstance(result, tuple) and len(result) == 2:
                    code, data = result
                    if isinstance(code, int):
                        entry = _lookup(code)
                        response_type = entry["response_type"]
                        if response_type is None or data is None:
                            return _wire_bytes(code, _RESPONSE_META_EMPTY, b"")
                        if isinstance(data, (dict, list)) and not entry["_has_response_validation"]:
                            if isinstance(data, list):
                                data = _convert_serializers(data)
                            return await serialize_json_data(data, entry, status_code=code)
                        return await entry["_default_response_handler"](data, status_code=code)

                # JSON() with explicit status: pick schema by its status_code.
                if isinstance(result, _JSONResponse):
                    entry = resolved_metas.get(result.status_code) or resolved_metas.get(...)
                    if entry is not None:
                        return await entry["_response_type_handler"](result)

                # Bare dict/list or any other type: use the default status schema.
                return await serialize_response(result, default_entry)

            def _dispatch_multi_sync(result: Any) -> ResponseWireV1:
                if isinstance(result, tuple) and len(result) == 2:
                    code, data = result
                    if isinstance(code, int):
                        entry = _lookup(code)
                        response_type = entry["response_type"]
                        if response_type is None or data is None:
                            return _wire_bytes(code, _RESPONSE_META_EMPTY, b"")
                        if isinstance(data, (dict, list)) and not entry["_has_response_validation"]:
                            if isinstance(data, list):
                                data = _convert_serializers(data)
                            return serialize_json_data_sync(data, entry, status_code=code)
                        return entry["_default_response_handler_sync"](data, status_code=code)

                # JSON() with explicit status: pick schema by its status_code.
                if isinstance(result, _JSONResponse):
                    entry = resolved_metas.get(result.status_code) or resolved_metas.get(...)
                    if entry is not None:
                        return entry["_response_type_handler_sync"](result)

                return serialize_response_sync(result, default_entry)

            async def execute_multi(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                request_state = request.setdefault("state", {})
                prebound_args = request_state.pop("_bolt_prebound_args", None)
                prebound_kwargs = request_state.pop("_bolt_prebound_kwargs", None)
                has_prebound = prebound_args is not None and prebound_kwargs is not None

                if has_prebound:
                    args, kwargs = prebound_args, prebound_kwargs
                elif injector_is_async:
                    args, kwargs = await injector(request)
                else:
                    args, kwargs = injector(request)

                if is_async:
                    return await _dispatch_multi_async(await handler(*args, **kwargs))
                if is_blocking:
                    result = await sync_to_thread(handler, *args, **kwargs)
                    return await sync_to_thread(_dispatch_multi_sync, result)
                return _dispatch_multi_sync(handler(*args, **kwargs))

            return execute_multi

        # Fast path: async handler without rust-prebound args (most common pattern).
        # Eliminates: state.setdefault, 2×state.pop, has_prebound check.
        if mode != "request_only" and is_async and not is_blocking and not has_rust_prebound and not injector_is_async:
            default_status = meta["default_status_code"]
            has_response_validation = meta["_has_response_validation"]

            # Detect trivially-async handlers: async def with no await statements.
            # These can be dispatched synchronously by driving the coroutine inline
            # via coro.send(None) → StopIteration, avoiding the entire async bridge.
            # Detection: check bytecode for GET_AWAITABLE opcode (emitted for every await).
            _original_fn = meta.get("_original_fn") or meta.get("handler")
            _trivially_async = False
            if _original_fn is not None:
                try:
                    _code = _original_fn.__code__
                    _trivially_async = not any(i.opname == "GET_AWAITABLE" for i in dis.get_instructions(_code))
                except (AttributeError, TypeError):
                    pass

            # Super-fast path: no compiled response validation → inline dict/list serialization
            # directly into the executor. Eliminates 2 async function call overheads
            # (serialize_response + serialize_json_data are both async but never suspend
            # for dict returns with no response model).
            _is_no_params = meta.get("handler_pattern") is HandlerPattern.NO_PARAMS

            if not has_response_validation:
                # Bind encoder method directly — skips the `if serializer is not None`
                # check in _json.encode() on every call.
                _encode = _json._ENCODER.encode
                _meta_json = _RESPONSE_META_JSON

                # Pre-compute at registration time whether _convert_serializers can be
                # skipped. When the return type is dict, list[Struct], list[dict], etc.
                # (i.e. NOT a Bolt Serializer), the conversion is always a no-op.
                _response_type = meta.get("response_type")
                _resp_origin = get_origin(_response_type) if _response_type else None
                _skip_convert = _resp_origin is list or _response_type is dict or _resp_origin is dict

                if _trivially_async:
                    # Sync executor for trivially-async handlers: drive coroutine inline.
                    # coro.send(None) immediately raises StopIteration since there are
                    # no await points. This bypasses the entire async bridge (~6-12μs).
                    if _is_no_params:
                        if _skip_convert:
                            # Ultra-fast: no params, no validation, result goes directly to encoder.
                            def execute_trivial_async_sync(
                                handler: Callable, request: dict[str, Any]
                            ) -> ResponseWireV1:
                                coro = handler()
                                try:
                                    coro.send(None)
                                except StopIteration as _e:
                                    return (default_status, _meta_json, _BODY_BYTES, _encode(_e.value))
                                else:
                                    coro.close()
                                    raise RuntimeError("Handler awaited unexpectedly in sync dispatch")
                        else:

                            def execute_trivial_async_sync(
                                handler: Callable, request: dict[str, Any]
                            ) -> ResponseWireV1:
                                coro = handler()
                                try:
                                    coro.send(None)
                                except StopIteration as _e:
                                    result = _e.value
                                else:
                                    coro.close()
                                    raise RuntimeError("Handler awaited unexpectedly in sync dispatch")
                                if isinstance(result, dict):
                                    return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                                if isinstance(result, list):
                                    result = _convert_serializers(result)
                                    return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                                return serialize_response_sync(result, meta)
                    else:
                        if _skip_convert:

                            def execute_trivial_async_sync(
                                handler: Callable, request: dict[str, Any]
                            ) -> ResponseWireV1:
                                args, kwargs = injector(request)
                                coro = handler(*args, **kwargs)
                                try:
                                    coro.send(None)
                                except StopIteration as _e:
                                    return (default_status, _meta_json, _BODY_BYTES, _encode(_e.value))
                                else:
                                    coro.close()
                                    raise RuntimeError("Handler awaited unexpectedly in sync dispatch")
                        else:

                            def execute_trivial_async_sync(
                                handler: Callable, request: dict[str, Any]
                            ) -> ResponseWireV1:
                                args, kwargs = injector(request)
                                coro = handler(*args, **kwargs)
                                try:
                                    coro.send(None)
                                except StopIteration as _e:
                                    result = _e.value
                                else:
                                    coro.close()
                                    raise RuntimeError("Handler awaited unexpectedly in sync dispatch")
                                if isinstance(result, dict):
                                    return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                                if isinstance(result, list):
                                    result = _convert_serializers(result)
                                    return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                                return serialize_response_sync(result, meta)

                    meta["_sync_executor"] = execute_trivial_async_sync

                if _is_no_params:
                    if _skip_convert:

                        async def execute_async_dict_fast(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            return (default_status, _meta_json, _BODY_BYTES, _encode(await handler()))
                    else:

                        async def execute_async_dict_fast(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            result = await handler()
                            if isinstance(result, dict):
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            if isinstance(result, list):
                                result = _convert_serializers(result)
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            return await serialize_response(result, meta)
                else:
                    if _skip_convert:

                        async def execute_async_dict_fast(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            args, kwargs = injector(request)
                            return (default_status, _meta_json, _BODY_BYTES, _encode(await handler(*args, **kwargs)))
                    else:

                        async def execute_async_dict_fast(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            args, kwargs = injector(request)
                            result = await handler(*args, **kwargs)
                            if isinstance(result, dict):
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            if isinstance(result, list):
                                result = _convert_serializers(result)
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            return await serialize_response(result, meta)

                return execute_async_dict_fast

            if _is_no_params:

                async def execute_async_no_prebound(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                    result = await handler()
                    return await serialize_response(result, meta)
            else:

                async def execute_async_no_prebound(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                    args, kwargs = injector(request)
                    result = await handler(*args, **kwargs)
                    return await serialize_response(result, meta)

            return execute_async_no_prebound

        # Fast path for sync non-blocking handler without rust-prebound args.
        if (
            mode != "request_only"
            and not is_async
            and not is_blocking
            and not has_rust_prebound
            and not injector_is_async
        ):
            default_status = meta["default_status_code"]
            has_response_validation = meta["_has_response_validation"]
            _is_no_params_sync = meta.get("handler_pattern") is HandlerPattern.NO_PARAMS

            if not has_response_validation:
                _encode = _json._ENCODER.encode
                _meta_json = _RESPONSE_META_JSON

                _response_type = meta.get("response_type")
                _resp_origin = get_origin(_response_type) if _response_type else None
                _skip_convert = _resp_origin is list or _response_type is dict or _resp_origin is dict

                # Plain (non-async) sync executor for Rust sync dispatch bypass.
                # Eliminates coroutine creation + into_future_with_locals overhead.
                if _is_no_params_sync:
                    if _skip_convert:

                        def execute_sync_dict_fast_plain(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            return (default_status, _meta_json, _BODY_BYTES, _encode(handler()))
                    else:

                        def execute_sync_dict_fast_plain(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            result = handler()
                            if isinstance(result, dict):
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            if isinstance(result, list):
                                result = _convert_serializers(result)
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            return serialize_response_sync(result, meta)
                else:
                    if _skip_convert:

                        def execute_sync_dict_fast_plain(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            args, kwargs = injector(request)
                            return (default_status, _meta_json, _BODY_BYTES, _encode(handler(*args, **kwargs)))
                    else:

                        def execute_sync_dict_fast_plain(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            args, kwargs = injector(request)
                            result = handler(*args, **kwargs)
                            if isinstance(result, dict):
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            if isinstance(result, list):
                                result = _convert_serializers(result)
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            return serialize_response_sync(result, meta)

                meta["_sync_executor"] = execute_sync_dict_fast_plain

                if _is_no_params_sync:
                    if _skip_convert:

                        async def execute_sync_dict_fast(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            return (default_status, _meta_json, _BODY_BYTES, _encode(handler()))
                    else:

                        async def execute_sync_dict_fast(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            result = handler()
                            if isinstance(result, dict):
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            if isinstance(result, list):
                                result = _convert_serializers(result)
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            return serialize_response_sync(result, meta)
                else:
                    if _skip_convert:

                        async def execute_sync_dict_fast(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            args, kwargs = injector(request)
                            return (default_status, _meta_json, _BODY_BYTES, _encode(handler(*args, **kwargs)))
                    else:

                        async def execute_sync_dict_fast(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
                            args, kwargs = injector(request)
                            result = handler(*args, **kwargs)
                            if isinstance(result, dict):
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            if isinstance(result, list):
                                result = _convert_serializers(result)
                                return (default_status, _meta_json, _BODY_BYTES, _encode(result))
                            return serialize_response_sync(result, meta)

                return execute_sync_dict_fast

        async def execute(handler: Callable, request: dict[str, Any]) -> ResponseWireV1:
            request_state = request.setdefault("state", {})

            if mode == "request_only":
                if is_async:
                    result = await handler(request)
                elif is_blocking:
                    result = await sync_to_thread(handler, request)
                else:
                    result = handler(request)
            else:
                if has_rust_prebound:
                    prebound_args = request_state.pop("_bolt_prebound_args", None)
                    prebound_kwargs = request_state.pop("_bolt_prebound_kwargs", None)
                    has_prebound = prebound_args is not None and prebound_kwargs is not None
                else:
                    has_prebound = False

                if has_prebound:
                    args, kwargs = prebound_args, prebound_kwargs
                elif injector_is_async:
                    args, kwargs = await injector(request)
                else:
                    args, kwargs = injector(request)

                if is_async:
                    result = await handler(*args, **kwargs)
                elif is_blocking:
                    result = await sync_to_thread(handler, *args, **kwargs)
                else:
                    result = handler(*args, **kwargs)

            if is_async:
                return await serialize_response(result, meta)

            if is_blocking or isinstance(result, QuerySet):
                return await sync_to_thread(serialize_response_sync, result, meta)
            return serialize_response_sync(result, meta)

        return execute

    def _handle_http_exception(self, he: HTTPException) -> Response:
        """Handle HTTPException and return response."""
        try:
            body = _json.encode({"detail": he.detail})
            headers = [("content-type", "application/json")]
        except Exception:
            body = str(he.detail).encode()
            headers = [("content-type", "text/plain; charset=utf-8")]

        if he.headers:
            headers.extend([(k.lower(), v) for k, v in he.headers.items()])

        return _wire_from_error_parts(int(he.status_code), headers, body)

    def _handle_generic_exception(self, e: Exception, request: dict[str, Any] = None) -> Response:
        """Handle generic exception using error_handlers module."""
        # Use the error handler which respects Django DEBUG setting
        status, headers, body = handle_exception(e, debug=None, request=request)  # debug will be checked dynamically
        return _wire_from_error_parts(status, headers, body)

    @staticmethod
    def _is_python_middleware_spec(middleware_spec: Any) -> bool:
        """Return True for middleware entries that execute in Python."""
        return not isinstance(middleware_spec, dict)

    @staticmethod
    def _clone_wrapper_middleware_spec(middleware_spec: Any) -> Any:
        """Clone supported middleware wrapper instances for per-route chains."""
        clone = getattr(middleware_spec, "clone", None)
        if callable(clone):
            return clone()

        # DjangoMiddleware wrapper
        if hasattr(middleware_spec, "middleware_class") and hasattr(middleware_spec, "init_kwargs"):
            return middleware_spec.__class__(middleware_spec.middleware_class, **middleware_spec.init_kwargs)

        # DjangoMiddlewareStack wrapper
        if hasattr(middleware_spec, "middleware_classes"):
            return middleware_spec.__class__(list(middleware_spec.middleware_classes))

        raise TypeError(
            f"Unsupported middleware wrapper instance '{type(middleware_spec).__name__}'. "
            "Pass a middleware class or supported DjangoMiddleware/DjangoMiddlewareStack wrapper."
        )

    def _wrap_middleware_spec(
        self,
        middleware_spec: Any,
        get_response: Callable[[Any], Any],
        *,
        clone_wrapper: bool = False,
    ) -> Callable[[Any], Any]:
        """Wrap a get_response callable with a normalized middleware spec."""
        if isinstance(middleware_spec, FunctionMiddlewareSpec):

            async def function_middleware(request):
                result = middleware_spec.func(request, get_response)
                if inspect.isawaitable(result):
                    return await result
                return result

            return function_middleware

        if hasattr(middleware_spec, "_create_middleware_instance"):
            wrapper_instance = (
                self._clone_wrapper_middleware_spec(middleware_spec) if clone_wrapper else middleware_spec
            )
            wrapper_instance._create_middleware_instance(get_response)
            return wrapper_instance

        if isinstance(middleware_spec, type):
            return middleware_spec(get_response)

        raise TypeError(
            f"Unsupported middleware entry '{middleware_spec!r}'. "
            "Expected middleware class, middleware wrapper, or function middleware spec."
        )

    def _build_middleware_chain(self, api: BoltAPI) -> Callable | None:
        """
        Build the cached global middleware chain for an API instance.

        Args:
            api: The BoltAPI instance to build chain for

        Returns:
            The outermost middleware callable.
        """

        # Route executors are request-scoped and injected via request.state.
        async def inner_handler(req):
            route_executor = req.state["_bolt_route_executor"]
            return await route_executor(req)

        chain = inner_handler
        for middleware_spec in reversed(api._middleware):
            if not self._is_python_middleware_spec(middleware_spec):
                continue
            chain = self._wrap_middleware_spec(middleware_spec, chain)

        return chain

    async def _execute_handler_as_middleware_response(
        self,
        handler: Callable,
        request: dict[str, Any],
        meta: dict[str, Any],
    ) -> MiddlewareResponse:
        """Execute handler using middleware semantics and return MiddlewareResponse."""
        response_tuple = await meta["_handler_executor"](handler, request)
        return MiddlewareResponse.from_tuple(response_tuple)

    def _build_route_executor(
        self,
        handler: Callable,
        meta: dict[str, Any],
    ) -> Callable[[Any], Any]:
        """Build per-handler route executor (router+route middleware + handler)."""
        router_middleware = meta.get("_router_middleware", [])
        route_middleware = meta.get("_route_middleware", getattr(handler, "__bolt_middleware__", []))
        combined_specs = [*router_middleware, *route_middleware]
        normalized_specs = normalize_middleware_specs(combined_specs, context="route", allow_function_middleware=True)

        async def execute_handler(req):
            return await self._execute_handler_as_middleware_response(handler, req, meta)

        chain = execute_handler
        for middleware_spec in reversed(normalized_specs):
            if not self._is_python_middleware_spec(middleware_spec):
                continue
            # Route executors are cached per handler, so wrapper instances must be cloned.
            chain = self._wrap_middleware_spec(middleware_spec, chain, clone_wrapper=True)

        return chain

    def _get_route_executor(
        self,
        api: BoltAPI,
        handler: Callable,
        meta: dict[str, Any],
    ) -> Callable[[Any], Any]:
        """Get or lazily build the cached route executor for a handler."""
        cached_executor = api._route_executor_cache.get(handler)
        if cached_executor is not None:
            return cached_executor

        with api._route_executor_lock:
            cached_executor = api._route_executor_cache.get(handler)
            if cached_executor is None:
                cached_executor = self._build_route_executor(handler, meta)
                api._route_executor_cache[handler] = cached_executor

        return cached_executor

    async def _dispatch_with_middleware(
        self,
        handler: Callable,
        request: dict[str, Any],
        handler_id: int,
        api: BoltAPI,
        meta: dict[str, Any],
    ) -> Response:
        """
        Execute global middleware + per-handler route executor.

        Args:
            handler: The route handler function
            request: The request dictionary
            handler_id: Handler ID
            api: The BoltAPI instance that owns this handler (may be sub-app)
            meta: Handler metadata
        """
        # Build global middleware chain once per API and cache it.
        if not api._middleware_chain_built:
            with api._middleware_chain_lock:
                if not api._middleware_chain_built:
                    api._middleware_chain = self._build_middleware_chain(api)
                    api._middleware_chain_built = True

        request_state = request.setdefault("state", {})

        # Store csrf_exempt in request.state for CSRF middleware to check.
        request_state["_csrf_exempt"] = meta.get("csrf_exempt", False)
        request_state["_bolt_route_executor"] = self._get_route_executor(api, handler, meta)

        try:
            middleware_response = await api._middleware_chain(request)
            if isinstance(middleware_response, MiddlewareResponse):
                return middleware_response.to_tuple()
            if isinstance(middleware_response, tuple):
                return MiddlewareResponse.from_tuple(middleware_response).to_tuple()
            raise TypeError(
                f"Middleware chain returned unsupported response type: {type(middleware_response).__name__}. "
                "Expected MiddlewareResponse or ResponseWireV1 tuple."
            )
        finally:
            if request_state:
                request_state.pop("_bolt_route_executor", None)

    async def _check_revocation(
        self,
        auth_context: dict[str, Any],
        revocation_handlers: dict[str, Callable],
    ) -> None:
        """Reject the request if the authenticated token's JTI is revoked."""
        handler = revocation_handlers.get(auth_context["auth_backend"])
        if handler is None:
            # Matched backend has no revocation (e.g., API-key auth on a
            # route where only JWT configured one).
            return

        # When auth_context is set, Rust guarantees auth_claims is too.
        jti = auth_context["auth_claims"].get("jti")
        if not jti:
            # Without a JTI we cannot identify which token to check.
            # Rejecting is safer than silently honoring.
            raise HTTPException(
                status_code=401,
                detail="Token missing 'jti' claim required for revocation",
            )
        if await handler(jti):
            raise HTTPException(status_code=401, detail="Token has been revoked")

    async def _dispatch(self, handler: Callable, request: dict[str, Any], handler_id: int = None) -> Response:
        """
        Optimized async dispatch that calls the handler and returns response tuple.

        Performance optimizations:
        - Unchecked metadata access (guaranteed to exist)
        - Inline user loading (eliminates function call overhead)
        - Pre-compiled argument injector (zero parameter binding overhead)
        - Streamlined execution flow (minimal branching)
        - Eliminated hasattr() checks via __init__ initialization
        - Zero logging overhead: access logging moved to Rust (Granian pattern),
          only exception logging remains here (BlackSheep pattern)

        Args:
            handler: The route handler function
            request: The request dictionary
            handler_id: Handler ID to lookup original API (for merged APIs)
        """
        # For merged APIs, use the original API's middleware chain and logging
        # This preserves per-API auth, middleware, and logging config (Litestar-style)
        # Note: _handler_api_map is always initialized in __init__ (no hasattr needed)
        original_api = self._handler_api_map.get(handler_id) if handler_id is not None else None

        try:
            # 1. Direct metadata access using handler_id (int key is faster than callable key)
            # Integer hashing is O(1) with minimal overhead vs callable hashing
            meta = self._handler_meta[handler_id]

            # 2. Lazy user loading using SimpleLazyObject (Django pattern)
            # User is only loaded from DB when request.user is actually accessed
            # Skip setting user=None — PyRequest.user getter already returns None.
            auth_context = request.get("auth")
            if auth_context:
                revocation_handlers = meta["_revocation_handlers"]
                if revocation_handlers is not None:
                    await self._check_revocation(auth_context, revocation_handlers)

                user_id = auth_context.get("user_id")
                if user_id:
                    backend_name = auth_context.get("auth_backend")
                    is_async_ctx = meta["is_async"]
                    request["user"] = SimpleLazyObject(
                        partial(load_user_sync, user_id, backend_name, auth_context, is_async_ctx)
                    )

            # 3. Check if we need to execute middleware
            # Middleware runs for:
            # - Global Python middleware on the owning app
            # - Router/route Python middleware on this handler
            middleware_owner = original_api if original_api is not None else self
            has_python_global_middleware = middleware_owner._has_python_global_middleware
            has_route_python_middleware = meta["_has_route_python_middleware"]
            api_with_middleware = (
                middleware_owner if (has_python_global_middleware or has_route_python_middleware) else None
            )

            if api_with_middleware:
                # Execute through middleware chain (Django-style)
                return await self._dispatch_with_middleware(handler, request, handler_id, api_with_middleware, meta)
            else:
                # Fast path: no middleware, execute pre-compiled handler executor directly.
                return await meta["_handler_executor"](handler, request)

        except HTTPException as he:
            return self._handle_http_exception(he)
        except Exception as e:
            # BlackSheep pattern: only log unhandled exceptions (rare path)
            if self._logging_middleware:
                self._logging_middleware.log_exception(request, e, exc_info=True)
            return self._handle_generic_exception(e, request=request)
        finally:
            # Auto-cleanup UploadFiles to prevent resource leaks
            # Only runs for handlers with file uploads (optimization: skip for 95%+ of requests)
            if meta["has_file_uploads"]:
                request_state = request.setdefault("state", {})
                upload_files = request_state.get("_upload_files", [])
                for upload in upload_files:
                    with suppress(Exception):
                        upload.close_sync()

    def _dispatch_sync(self, handler: Callable, request: dict[str, Any], handler_id: int = None) -> Response:
        """
        Synchronous dispatch for sync handlers without middleware/signals.

        Called directly from Rust via dispatch_sync.call1() - no coroutine creation,
        no into_future_with_locals, no asyncio event loop polling. This eliminates
        ~6-12us of async bridge overhead per request.

        Zero logging overhead: access logging (timing, method/path/status) is handled
        in Rust via const generic (compiler-eliminated when off). Only unhandled
        exception logging remains here (BlackSheep pattern — rare path only).

        Prerequisites (checked at registration time, stored in can_sync_dispatch flag):
        - Handler has a _sync_executor (sync, non-blocking, simple params)
        - No Python middleware (global or route-level)
        - No Django middleware
        - No signals
        """
        try:
            meta = self._handler_meta[handler_id]

            # Lazy user loading: only set when auth context has a user_id.
            # Skip setting user=None — PyRequest.user getter already returns None.
            auth_context = request.get("auth")
            if auth_context:
                user_id = auth_context.get("user_id")
                if user_id:
                    backend_name = auth_context.get("auth_backend")
                    request["user"] = SimpleLazyObject(
                        partial(load_user_sync, user_id, backend_name, auth_context, False)
                    )

            # Call pre-compiled sync executor directly (no coroutine, no await)
            return meta["_sync_executor"](handler, request)

        except HTTPException as he:
            return self._handle_http_exception(he)
        except Exception as e:
            # BlackSheep pattern: only log unhandled exceptions (rare path)
            if self._logging_middleware:
                self._logging_middleware.log_exception(request, e, exc_info=True)
            return self._handle_generic_exception(e, request=request)
        finally:
            if meta["has_file_uploads"]:
                request_state = request.setdefault("state", {})
                upload_files = request_state.get("_upload_files", [])
                for upload in upload_files:
                    with suppress(Exception):
                        upload.close_sync()

    def _get_openapi_schema(self) -> dict[str, Any]:
        """Get or generate OpenAPI schema.

        Returns:
            OpenAPI schema as dictionary.
        """
        if self._openapi_schema is None:
            generator = SchemaGenerator(self, self._openapi_config)
            openapi = generator.generate()
            self._openapi_schema = openapi.to_schema()

        return self._openapi_schema

    def _register_openapi_routes(self) -> None:
        """Register OpenAPI documentation routes.

        Delegates to OpenAPIRouteRegistrar for cleaner separation of concerns.
        """

        registrar = OpenAPIRouteRegistrar(self)
        registrar.register_routes()

    def _register_admin_routes(self, host: str = "localhost", port: int = 8000) -> None:
        """Register Django admin as an ASGI mount."""

        registrar = AdminRouteRegistrar(self)
        registrar.register_routes(host, port)

    def _register_static_routes(self) -> None:
        """Register static file serving routes for Django admin.

        Delegates to StaticRouteRegistrar for cleaner separation of concerns.
        """

        registrar = StaticRouteRegistrar(self)
        registrar.register_routes()

    def _register_auth_backends(self) -> None:
        """
        Register authentication backends for user resolution.

        Scans all handler middleware metadata to find unique auth backends,
        then registers them for request.user lazy loading.
        """
        registered = set()

        for _handler_id, metadata in self._handler_middleware.items():
            # Get stored backend instances (stored during route decoration)
            backend_instances = metadata.get("_auth_backend_instances", [])
            for backend_instance in backend_instances:
                backend_type = backend_instance.scheme_name
                if backend_type and backend_type not in registered:
                    registered.add(backend_type)
                    register_auth_backend(backend_type, backend_instance)

    def mount(self, path: str, app: BoltAPI) -> None:
        """
        Mount a sub-application at a given path (FastAPI-style).

        The mounted app's routes are copied to this app with the path prefix prepended.
        Each sub-app maintains its own middleware, auth, and configuration.

        Usage:
            # Create a sub-application with its own middleware
            middleware_app = BoltAPI(
                middleware=[RequestIdMiddleware, TenantMiddleware],
                django_middleware=True,
            )

            @middleware_app.get("/demo")
            async def demo_endpoint(request: Request):
                return {"status": "ok"}

            # Mount it at /middleware
            api = BoltAPI()
            api.mount("/middleware", middleware_app)

            # Results in: GET /middleware/demo

        Args:
            path: URL prefix for all routes in the sub-app (e.g., "/api/v2")
            app: BoltAPI instance to mount

        Note:
            Unlike include_router(), mount() preserves the sub-app's middleware
            and configuration independently. This is similar to FastAPI's mount()
            for sub-applications.
        """
        if not isinstance(app, BoltAPI):
            raise TypeError(
                f"mount() expects a BoltAPI instance, got {type(app).__name__}. "
                f"Use include_router() for Router instances."
            )

        # Normalize path prefix: ensure leading slash, strip trailing slash
        mount_path = "/" + path.strip("/") if path else ""

        # Copy routes from sub-app to this app with path prefix
        for method, route_path, handler_id, handler in app._routes:
            # Compute new path with mount prefix and normalize using parent's trailing_slash setting
            new_path = _normalize_path(mount_path + route_path, self.trailing_slash)

            # Create new handler ID in parent's namespace
            new_handler_id = self._next_handler_id
            self._next_handler_id += 1

            # Register route in parent
            self._routes.append((method, new_path, new_handler_id, handler))
            self._handlers[new_handler_id] = handler

            # Copy handler metadata (now keyed by handler_id for performance)
            if handler_id in app._handler_meta:
                self._handler_meta[new_handler_id] = app._handler_meta[handler_id]

            # Copy middleware metadata (with path updated)
            if handler_id in app._handler_middleware:
                middleware_meta = app._handler_middleware[handler_id].copy()
                middleware_meta["path"] = new_path
                self._handler_middleware[new_handler_id] = middleware_meta

            # Track which API owns this handler (for logging, etc.)
            # _handler_api_map is always initialized in __init__
            self._handler_api_map[new_handler_id] = app

        # Copy WebSocket routes
        for ws_path, handler_id, handler in app._websocket_routes:
            # Compute new path with mount prefix and normalize using parent's trailing_slash setting
            new_path = _normalize_path(mount_path + ws_path, self.trailing_slash)

            new_handler_id = self._next_handler_id
            self._next_handler_id += 1

            self._websocket_routes.append((new_path, new_handler_id, handler))
            self._handlers[new_handler_id] = handler

            # Copy handler metadata (now keyed by handler_id for performance)
            if handler_id in app._handler_meta:
                self._handler_meta[new_handler_id] = app._handler_meta[handler_id]

            if handler_id in app._handler_middleware:
                middleware_meta = app._handler_middleware[handler_id].copy()
                middleware_meta["path"] = new_path
                self._handler_middleware[new_handler_id] = middleware_meta

            # Track which API owns this handler (for logging, etc.)
            # _handler_api_map is always initialized in __init__
            self._handler_api_map[new_handler_id] = app

        for asgi_prefix, asgi_app in app._asgi_mounts:
            if mount_path:
                if asgi_prefix == "/":
                    new_asgi_prefix = _normalize_mount_prefix(mount_path)
                else:
                    new_asgi_prefix = _normalize_mount_prefix(mount_path + asgi_prefix)
            else:
                new_asgi_prefix = asgi_prefix

            if new_asgi_prefix in self._asgi_mount_prefixes:
                raise ValueError(f"Duplicate ASGI mount prefix: {new_asgi_prefix}")
            self._asgi_mount_prefixes.add(new_asgi_prefix)
            self._asgi_mounts.append((new_asgi_prefix, asgi_app))

        # Remove sub-app from global registry (parent handles its routes now)
        if app in _BOLT_API_REGISTRY:
            _BOLT_API_REGISTRY.remove(app)

    def mount_asgi(self, path: str, app: Callable[..., Any]) -> None:
        """Mount an ASGI app at a static prefix (evaluated after Bolt route miss)."""
        if not callable(app):
            raise TypeError(f"mount_asgi() expects a callable ASGI application, got {type(app).__name__}")

        mount_path = _normalize_mount_prefix(path)

        if self.prefix:
            if mount_path == "/":
                mount_path = _normalize_mount_prefix(self.prefix)
            else:
                mount_path = _normalize_mount_prefix(self.prefix + mount_path)

        if mount_path in self._asgi_mount_prefixes:
            raise ValueError(f"Duplicate ASGI mount prefix: {mount_path}")

        self._asgi_mount_prefixes.add(mount_path)
        self._asgi_mounts.append((mount_path, app))

    def mount_django(self, path: str, app: Any | None = None, *, clear_root_path: bool = False) -> None:
        """Mount Django's ASGI app at a path prefix (convenience wrapper over mount_asgi).

        Args:
            path: URL prefix (e.g. ``"/admin"``).
            app: ASGI callable; defaults to ``get_asgi_application()``.
            clear_root_path: Set ``root_path=""`` before Django sees the scope.
                Required when URL patterns already include the mount prefix
                (e.g. ``/admin/...``).
        """
        asgi_app = app
        if asgi_app is None:
            asgi_app = get_asgi_application()

        async def django_mount_wrapper(scope, receive, send):
            django_scope = _rewrite_scope_for_django_mount(scope)
            if clear_root_path:
                if django_scope is scope:
                    django_scope = dict(scope)
                django_scope["root_path"] = ""
            root_path = django_scope.get("root_path") or ""

            async def django_send(message):
                rewritten = _rewrite_django_mount_redirect_message(message, root_path)
                await send(rewritten)

            await asgi_app(django_scope, receive, django_send)

        self.mount_asgi(path, django_mount_wrapper)

    def include_router(self, router: Router, prefix: str = "") -> None:
        """
        Include a Router's routes into this API.

        This method copies all routes from the router to this API, applying
        the optional prefix. Router-level middleware, auth, and guards are
        merged with route-specific settings.

        Usage:
            from django_bolt import BoltAPI, Router

            users_router = Router(prefix="/users", tags=["users"])

            @users_router.get("")
            async def list_users():
                return []

            @users_router.get("/{user_id}")
            async def get_user(user_id: int):
                return {"id": user_id}

            api = BoltAPI()
            api.include_router(users_router)
            # Results in: GET /users, GET /users/{user_id}

            # With additional prefix
            api.include_router(users_router, prefix="/api/v1")
            # Results in: GET /api/v1/users, GET /api/v1/users/{user_id}

        Args:
            router: Router instance containing routes to include
            prefix: Additional URL prefix to prepend (combined with router's prefix)
        """
        if not isinstance(router, Router):
            raise TypeError(
                f"include_router() expects a Router instance, got {type(router).__name__}. "
                f"Use mount() for BoltAPI sub-applications."
            )

        # Get all routes from router (including nested routers)
        all_routes = router.get_all_routes()

        for method, route_path, handler, meta in all_routes:
            route_meta = dict(meta)

            # Compute full path with optional prefix
            full_path = prefix.rstrip("/") + route_path if prefix else route_path

            # Extract route-specific overrides from meta
            route_auth = route_meta.pop("auth", None)
            route_guards = route_meta.pop("guards", None)
            route_tags = route_meta.pop("tags", None)
            route_router_middleware = route_meta.pop("_router_middleware", [])

            # Register route with merged settings and preserved router middleware.
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                continue

            decorator = self._route_decorator(
                method.upper(),
                full_path,
                auth=route_auth,
                guards=route_guards,
                tags=route_tags,
                _router_middleware=route_router_middleware,
                **route_meta,
            )

            # Apply decorator to register handler
            decorator(handler)

    @property
    def _has_lifespan(self) -> bool:
        """Check if this API has a lifespan context manager."""
        return self._lifespan_context is not None
