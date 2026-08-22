"""
Middleware system for Django-Bolt.

Provides both decorator-based and class-based middleware approaches.
Middleware can be applied globally to all routes or selectively to specific routes.

Performance is the utmost priority - the middleware system is designed for zero overhead:
- Hot-path operations (CORS, rate limiting, JWT validation) run in Rust
- Python middleware only runs when explicitly configured
- Lazy evaluation and minimal allocations
- Pattern matching compiled once at startup
"""

from __future__ import annotations

import inspect
import logging
import re
import time
import traceback
import uuid
import warnings
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from re import Pattern
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    Union,
    runtime_checkable,
)

from ..exceptions import HTTPException

if TYPE_CHECKING:
    from ..request import Request
    from ..responses import Response


# ═══════════════════════════════════════════════════════════════════════════
# Type Aliases
# ═══════════════════════════════════════════════════════════════════════════


# Type alias for get_response function (Django-style)
GetResponse = Callable[["Request"], Awaitable["Response"]]


# Type alias for middleware class (Django-style: takes get_response in __init__)
MiddlewareType = Union[
    "MiddlewareProtocol",
    type["BaseMiddleware"],
]


@dataclass(frozen=True, slots=True)
class FunctionMiddlewareSpec:
    """Marker for function-style middleware attached to route handlers."""

    func: Callable


def _ensure_handler_middleware_list(handler: Callable) -> list[Any]:
    """Get/create per-handler middleware metadata list."""
    if not hasattr(handler, "__bolt_middleware__"):
        handler.__bolt_middleware__ = []
    return handler.__bolt_middleware__


def _is_function_middleware_callable(spec: Any) -> bool:
    """Return True only for plain function-style middleware callables."""
    return inspect.isfunction(spec) or inspect.ismethod(spec) or inspect.isbuiltin(spec)


def normalize_middleware_specs(
    middleware_specs: list[Any] | tuple[Any, ...] | None,
    *,
    context: str,
    allow_function_middleware: bool = False,
) -> list[Any]:
    """
    Normalize and validate middleware specs.

    Allowed forms:
    - Middleware classes (Django-style __init__(get_response))
    - DjangoMiddleware / DjangoMiddlewareStack wrapper instances
    - Dict middleware metadata (Rust-handled middleware)
    - FunctionMiddlewareSpec (route-level only)
    - Raw middleware functions (route-level only; converted to FunctionMiddlewareSpec)
    """
    normalized: list[Any] = []
    if not middleware_specs:
        return normalized

    for spec in middleware_specs:
        if isinstance(spec, dict):
            normalized.append(spec)
            continue

        if isinstance(spec, FunctionMiddlewareSpec):
            if not allow_function_middleware:
                raise TypeError(
                    f"{context} middleware does not support function-style middleware. "
                    "Use middleware classes or DjangoMiddleware/DjangoMiddlewareStack wrappers."
                )
            normalized.append(spec)
            continue

        # Fail-fast for plain middleware instances.
        if isinstance(spec, (BaseMiddleware, Middleware)):
            raise TypeError(
                f"{context} middleware does not accept middleware instances ({type(spec).__name__}). "
                "Pass middleware classes instead, e.g. middleware=[MyMiddleware]."
            )

        # Django middleware wrappers are instance-based by design.
        if hasattr(spec, "_create_middleware_instance"):
            normalized.append(spec)
            continue

        if isinstance(spec, type):
            normalized.append(spec)
            continue

        if callable(spec):
            if allow_function_middleware and _is_function_middleware_callable(spec):
                normalized.append(FunctionMiddlewareSpec(spec))
                continue
            if not isinstance(spec, type):
                raise TypeError(
                    f"{context} middleware does not accept middleware instances ({type(spec).__name__}). "
                    "Pass middleware classes instead, e.g. middleware=[MyMiddleware]."
                )
            raise TypeError(
                f"{context} middleware entry '{spec}' must be a middleware class "
                "or DjangoMiddleware/DjangoMiddlewareStack wrapper."
            )

        raise TypeError(
            f"Unsupported {context} middleware entry: {spec!r}. "
            "Expected middleware class, DjangoMiddleware wrapper, dict config, or function middleware (route-level only)."
        )

    return normalized


# ═══════════════════════════════════════════════════════════════════════════
# Middleware Protocol
# ═══════════════════════════════════════════════════════════════════════════


@runtime_checkable
class MiddlewareProtocol(Protocol):
    """
    Protocol for Django-Bolt middleware (Django-style pattern).

    Uses the same pattern as Django middleware:
    - __init__(get_response): Receives the next middleware/handler in chain
    - __call__(request): Processes the request

    This unified pattern works for both Django and Bolt middleware.

    Example:
        class TimingMiddleware:
            def __init__(self, get_response):
                self.get_response = get_response

            async def __call__(self, request: Request) -> Response:
                start = time.time()
                request.state["start_time"] = start

                response = await self.get_response(request)

                elapsed = time.time() - start
                response.headers["X-Response-Time"] = f"{elapsed:.3f}s"
                return response

    The middleware can:
    - Modify the request before passing it on
    - Short-circuit by returning a response directly
    - Modify the response after the handler executes
    - Add data to request.state for downstream handlers
    """

    def __init__(self, get_response: GetResponse) -> None: ...

    async def __call__(self, request: Request) -> Response: ...


# ═══════════════════════════════════════════════════════════════════════════
# Base Middleware Class
# ═══════════════════════════════════════════════════════════════════════════


class BaseMiddleware(ABC):
    """
    Base class for Django-Bolt middleware (Django-style pattern).

    Uses the same pattern as Django middleware:
    - __init__(get_response): Receives the next middleware/handler in chain
    - __call__(request): Processes the request

    Provides:
    - Path exclusion patterns (glob-style wildcards)
    - Method filtering

    Example:
        class AuthMiddleware(BaseMiddleware):
            exclude_paths = ["/health", "/metrics", "/docs/*"]
            exclude_methods = ["OPTIONS"]

            async def process_request(self, request: Request) -> Response:
                if not request.headers.get("authorization"):
                    raise HTTPException(401, "Unauthorized")
                return await self.get_response(request)

    Performance:
        - Pattern matching is compiled once at instantiation
        - Exclusion checks are O(1) for methods, O(n) regex match for paths
        - No allocations in hot path
    """

    # Paths to exclude from this middleware (supports wildcards)
    exclude_paths: list[str] | None = None

    # HTTP methods to exclude
    exclude_methods: list[str] | None = None

    # Compiled exclusion pattern (set during __init__)
    _exclude_pattern: Pattern | None = None
    _exclude_methods_set: set[str] | None = None

    def __init__(self, get_response: GetResponse) -> None:
        """
        Initialize middleware with get_response (Django-style).

        Args:
            get_response: The next middleware/handler in the chain
        """
        self.get_response = get_response

        # Compile path exclusion patterns once
        if self.exclude_paths:
            patterns = []
            for path in self.exclude_paths:
                # Convert glob patterns to regex
                pattern = re.escape(path).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
                patterns.append(f"^{pattern}$")
            self._exclude_pattern = re.compile("|".join(patterns))

        # Convert method list to set for O(1) lookup
        if self.exclude_methods:
            self._exclude_methods_set = {m.upper() for m in self.exclude_methods}

    async def __call__(self, request: Request) -> Response:
        """Process request, checking exclusions first."""
        # Check exclusions (fast path)
        if self._should_skip(request):
            return await self.get_response(request)

        return await self.process_request(request)

    def _should_skip(self, request: Request) -> bool:
        """
        Check if this request should skip the middleware.

        Performance: O(1) for method check, O(n) regex for path check.
        """
        # Check method exclusion (O(1) set lookup)
        if self._exclude_methods_set and request.method in self._exclude_methods_set:
            return True

        # Check path exclusion (regex match)
        return bool(self._exclude_pattern and self._exclude_pattern.match(request.path))

    @abstractmethod
    async def process_request(self, request: Request) -> Response:
        """
        Process the request. Override this in subclasses.

        Call self.get_response(request) to continue the chain.

        Args:
            request: The incoming request

        Returns:
            Response object
        """
        ...


# ═══════════════════════════════════════════════════════════════════════════
# Simple Middleware Class (convenience alias)
# ═══════════════════════════════════════════════════════════════════════════


class Middleware(ABC):
    """
    Simple middleware base class (Django-style pattern).

    Alias for BaseMiddleware without exclusion patterns.
    Use this for simple middleware that doesn't need path/method filtering.

    Example:
        class MyMiddleware(Middleware):
            async def process_request(self, request: Request) -> Response:
                # Do something before
                response = await self.get_response(request)
                # Do something after
                return response
    """

    def __init__(self, get_response: GetResponse) -> None:
        """Initialize with get_response (next in chain)."""
        self.get_response = get_response

    @abstractmethod
    async def process_request(self, request: Request) -> Response:
        """
        Process the request.

        Call self.get_response(request) to continue the chain.

        Args:
            request: The incoming request object

        Returns:
            Response object
        """
        pass

    async def __call__(self, request: Request) -> Response:
        """Process request through middleware."""
        return await self.process_request(request)


# ═══════════════════════════════════════════════════════════════════════════
# Middleware Decorators
# ═══════════════════════════════════════════════════════════════════════════


def middleware(*args, **kwargs):
    """
    Decorator to attach middleware to a route handler.

    Can be used as:
    - @middleware(MyMiddlewareClass)
    - @middleware(cors={"origins": ["*"]})
    - @middleware(skip=["auth"])

    Example:
        @api.post("/upload")
        @middleware(
            ValidateContentTypeMiddleware,
            FileSizeLimitMiddleware,
        )
        async def upload_file(request: Request) -> dict:
            return {"uploaded": True}
    """

    def attach_middleware(handler: Callable, middleware_spec: Any) -> Callable:
        handler_middleware = _ensure_handler_middleware_list(handler)
        handler_middleware.append(middleware_spec)
        return handler

    # @middleware used directly on a function-style middleware declaration:
    #     @middleware
    #     async def my_middleware(request, call_next): ...
    # Returns a route decorator that attaches function middleware to handlers.
    if len(args) == 1 and callable(args[0]) and not kwargs and not isinstance(args[0], (type,)):
        fn_spec = normalize_middleware_specs([args[0]], context="route", allow_function_middleware=True)[0]

        def route_decorator(handler: Callable) -> Callable:
            return attach_middleware(handler, fn_spec)

        return route_decorator

    def decorator(handler: Callable) -> Callable:
        normalized = normalize_middleware_specs(args, context="route", allow_function_middleware=True)
        for spec in normalized:
            attach_middleware(handler, spec)

        # Support dict-style middleware metadata (e.g. @middleware(type="cors", ...))
        if kwargs:
            attach_middleware(handler, kwargs)

        return handler

    return decorator


def rate_limit(rps: int = 100, burst: int | None = None, key: str = "ip"):
    """
    Rate limiting decorator (Rust-accelerated).

    This middleware is handled in Rust for maximum performance.
    No Python overhead in the hot path.

    With `key="ip"`, Bolt keys on the peer address of the connection. A client
    can send `X-Forwarded-For` itself, so Bolt ignores that header by default.
    Behind a proxy, list the proxy in `settings.BOLT_TRUSTED_PROXIES`:

        BOLT_TRUSTED_PROXIES = ["10.0.0.0/8"]

    Bolt then reads `X-Forwarded-For` from the right and takes the first entry
    that is not a listed proxy. Without the setting, every caller behind the
    proxy shares one bucket.

    Args:
        rps: Requests per second limit
        burst: Burst capacity (defaults to 2x rps)
        key: Rate limit key strategy ("ip", "user", "api_key", or header name)

    Example:
        @api.get("/api/data")
        @rate_limit(rps=1000, burst=2000, key="ip")
        async def get_data(request: Request) -> dict:
            return {"data": [...]}
    """

    def decorator(func):
        if not hasattr(func, "__bolt_middleware__"):
            func.__bolt_middleware__ = []
        func.__bolt_middleware__.append({"type": "rate_limit", "rps": rps, "burst": burst or rps * 2, "key": key})
        return func

    return decorator


def cors(
    origins: list[str] | str = None,
    methods: list[str] = None,
    headers: list[str] = None,
    credentials: bool = False,
    max_age: int = 3600,
):
    """
    CORS configuration decorator (Rust-accelerated).

    This middleware is handled in Rust for maximum performance.
    CORS preflight requests are handled without Python overhead.

    Args:
        origins: Allowed origins (REQUIRED). Use ["*"] for all origins, or specific origins
                 like ["https://example.com"]. For global config, use Django settings instead.
        methods: Allowed methods (default: GET, POST, PUT, PATCH, DELETE, OPTIONS)
        headers: Allowed headers
        credentials: Allow credentials (cannot be combined with wildcard "*")
        max_age: Preflight cache duration in seconds (default: 3600)

    Examples:
        @cors(origins=["https://example.com"])
        async def my_endpoint(): ...

        @cors(origins=["*"])  # Allow all origins
        async def public_endpoint(): ...

        @cors(origins=["https://app.example.com"], credentials=True)
        async def with_cookies(): ...

    Raises:
        ValueError: If origins is not specified (empty @cors() is not allowed)
    """

    def decorator(func):
        # SECURITY: Require explicit origins - empty @cors() is a common mistake
        if origins is None:
            raise ValueError(
                "@cors() requires 'origins' argument. Examples:\n"
                "  @cors(origins=['https://example.com'])  # Specific origin\n"
                "  @cors(origins=['*'])  # Allow all origins\n"
                "\n"
                "If you want to use global CORS settings from Django (CORS_ALLOWED_ORIGINS),\n"
                "simply remove the @cors decorator - global config applies automatically."
            )

        if not hasattr(func, "__bolt_middleware__"):
            func.__bolt_middleware__ = []

        # Parse origins
        origin_list = origins if isinstance(origins, list) else [origins]

        # SECURITY: Validate wildcard + credentials
        if "*" in origin_list and credentials:
            warnings.warn(
                "CORS misconfiguration: Cannot use wildcard '*' with credentials=True. "
                "This violates the CORS specification. Please specify explicit origins.",
                RuntimeWarning,
                stacklevel=2,
            )

        func.__bolt_middleware__.append(
            {
                "type": "cors",
                "origins": origin_list,
                "methods": methods or ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "QUERY"],
                "headers": headers,
                "credentials": credentials,
                "max_age": max_age,
            }
        )
        return func

    return decorator


def skip_middleware(*middleware_names: str):
    """
    Skip specific middleware for this route.

    Args:
        middleware_names: Names of middleware to skip. Use "*" to skip all.

    Examples:
        @api.get("/no-compression")
        @skip_middleware("compression")
        async def no_compress():
            return {"data": "large response without compression"}

        @api.get("/raw")
        @skip_middleware("*")
        async def raw_endpoint():
            return {"raw": True}

        @api.get("/minimal")
        @skip_middleware("cors", "compression", "TimingMiddleware")
        async def minimal():
            return {"fast": True}
    """

    def decorator(func):
        if not hasattr(func, "__bolt_skip_middleware__"):
            func.__bolt_skip_middleware__ = set()
        func.__bolt_skip_middleware__.update(middleware_names)
        return func

    return decorator


def no_compress(func):
    """
    Disable compression for this route.

    Shorthand for @skip_middleware("compression").

    Examples:
        @api.get("/stream")
        @no_compress
        async def stream_data():
            # Compression would slow down streaming
            return StreamingResponse(...)
    """
    return skip_middleware("compression")(func)


# ═══════════════════════════════════════════════════════════════════════════
# Built-in Python Middleware
# ═══════════════════════════════════════════════════════════════════════════


class TimingMiddleware(BaseMiddleware):
    """
    Adds request timing information (Django-style).

    Adds to request.state:
        - request_id: Unique request identifier
        - start_time: Request start timestamp

    Adds response headers:
        - X-Request-ID: Request identifier
        - X-Response-Time: Time taken in seconds

    Example:
        api = BoltAPI(middleware=[TimingMiddleware])

        @api.get("/")
        async def index(request: Request) -> dict:
            request_id = request.state.get("request_id")
            return {"request_id": request_id}
    """

    async def process_request(self, request: Request) -> Response:
        request_id = str(uuid.uuid4())
        start_time = time.perf_counter()

        request.state["request_id"] = request_id
        request.state["start_time"] = start_time
        response = await self.get_response(request)

        elapsed = time.perf_counter() - start_time
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{elapsed:.4f}s"

        return response


class LoggingMiddleware(BaseMiddleware):
    """
    Logs request and response information (Django-style).

    Configurable log levels and formats.
    Excludes health/metrics endpoints by default.

    Example:
        api = BoltAPI(middleware=[LoggingMiddleware])
    """

    exclude_paths = ["/health", "/metrics", "/docs", "/openapi.json"]

    def __init__(
        self,
        get_response: GetResponse,
        logger: logging.Logger | None = None,
        log_body: bool = False,
        log_headers: bool = False,
        log_level: int = logging.INFO,
    ):
        super().__init__(get_response)
        self.logger = logger or logging.getLogger("django_bolt.requests")
        self.log_body = log_body
        self.log_headers = log_headers
        self.log_level = log_level

    async def process_request(self, request: Request) -> Response:
        # Log request
        log_data = {
            "method": request.method,
            "path": request.path,
        }
        if request.query:
            log_data["query"] = dict(request.query)
        if self.log_headers:
            log_data["headers"] = dict(request.headers)
        if self.log_body and request.body:
            log_data["body_size"] = len(request.body)

        self.logger.log(self.log_level, f"Request: {log_data}")

        # Process request
        start_time = time.perf_counter()
        response = await self.get_response(request)
        elapsed = time.perf_counter() - start_time

        # Log response
        self.logger.log(
            self.log_level, f"Response: {response.status_code} for {request.method} {request.path} ({elapsed:.4f}s)"
        )

        return response


class ErrorHandlerMiddleware(BaseMiddleware):
    """
    Global error handler middleware (Django-style).

    Catches exceptions and converts them to appropriate HTTP responses.
    Should be one of the first middleware in the chain.

    Example:
        api = BoltAPI(middleware=[ErrorHandlerMiddleware])
    """

    def __init__(self, get_response: GetResponse, debug: bool = False):
        super().__init__(get_response)
        self.debug = debug
        self.logger = logging.getLogger("django_bolt.errors")

    async def process_request(self, request: Request) -> Response:
        try:
            return await self.get_response(request)
        except HTTPException:
            raise  # Let HTTP exceptions pass through
        except Exception as e:
            self.logger.exception(f"Unhandled exception: {e}")

            detail = traceback.format_exc() if self.debug else "Internal Server Error"

            raise HTTPException(500, detail) from None


__all__ = [
    # Protocols and base classes
    "MiddlewareProtocol",
    "BaseMiddleware",
    "Middleware",
    "GetResponse",
    "MiddlewareType",
    "FunctionMiddlewareSpec",
    "normalize_middleware_specs",
    # Decorators
    "middleware",
    "rate_limit",
    "cors",
    "skip_middleware",
    "no_compress",
    # Built-in middleware (Python)
    "TimingMiddleware",
    "LoggingMiddleware",
    "ErrorHandlerMiddleware",
]
