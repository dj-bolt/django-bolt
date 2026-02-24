"""
Django middleware adapter for Django-Bolt.

Provides the DjangoMiddleware class that wraps Django middleware classes
to work with Django-Bolt's async middleware chain.

Performance considerations:
- Middleware instance is created ONCE at registration time (not per-request)
- Uses contextvars to bridge async call_next without per-request instantiation
- Conversion between Bolt Request and Django HttpRequest is lazy where possible
- Django request attributes are synced back only when needed
- Uses sync_to_async for Django operations that may touch the database
"""

from __future__ import annotations

import contextlib
import contextvars
import io
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from asgiref.sync import async_to_sync

from ..middleware_response import MiddlewareResponse

try:
    from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
    from django.http import HttpRequest, HttpResponse, QueryDict
    from django.utils.module_loading import import_string

    DJANGO_AVAILABLE = True
except ImportError:
    DJANGO_AVAILABLE = False
    HttpRequest = None
    HttpResponse = None
    QueryDict = None
    import_string = None
    sync_to_async = None
    iscoroutinefunction = None
    markcoroutinefunction = None

# Lazy singleton for empty QueryDict - avoids requiring Django settings at import time
_EMPTY_QUERYDICT = None


def _get_empty_querydict():
    """Get the empty QueryDict singleton, creating it lazily on first access."""
    global _EMPTY_QUERYDICT
    if _EMPTY_QUERYDICT is None:
        _EMPTY_QUERYDICT = QueryDict()
    return _EMPTY_QUERYDICT


if TYPE_CHECKING:
    from ..request import Request
    from ..responses import Response


# Context variable to hold per-request state for the get_response bridge
# This allows middleware instances to be created once at startup while
# still having access to the correct call_next at request time
_request_context: contextvars.ContextVar[dict] = contextvars.ContextVar("_django_middleware_request_context")


class DjangoMiddleware:
    """
    Wraps a Django middleware class to work with Django-Bolt.

    Follows Django's middleware pattern:
    - __init__(get_response): Called ONCE when middleware chain is built
    - __call__(request): Called for each request

    Supports both old-style (process_request/process_response) and
    new-style (callable) Django middleware patterns.

    Performance:
        - Middleware instance is created when chain is built (not per-request)
        - Request conversion is done once per middleware in the chain
        - Uses sync_to_async for database operations

    Examples:
        # Wrap Django's built-in middleware
        from django.contrib.auth.middleware import AuthenticationMiddleware
        from django.contrib.sessions.middleware import SessionMiddleware

        api = BoltAPI(
            middleware=[
                DjangoMiddleware(SessionMiddleware),
                DjangoMiddleware(AuthenticationMiddleware),
            ]
        )

        # Wrap by import path string
        api = BoltAPI(
            middleware=[
                DjangoMiddleware("django.contrib.sessions.middleware.SessionMiddleware"),
                DjangoMiddleware("myapp.middleware.CustomMiddleware"),
            ]
        )

    Note:
        Order matters! Django middlewares should be in the same order as
        they would be in Django's MIDDLEWARE setting.
    """

    __slots__ = (
        "middleware_class",
        "init_kwargs",
        "get_response",
        "_middleware_instance",
        "_middleware_is_async",
    )

    def __init__(self, middleware_class_or_get_response: type | str | Callable, **init_kwargs: Any):
        """
        Initialize the Django middleware wrapper.

        This can be called in two ways:
        1. DjangoMiddleware(SomeMiddlewareClass) - stores the class for later instantiation
        2. DjangoMiddleware(get_response) - called by chain building, instantiates the middleware

        Args:
            middleware_class_or_get_response: Django middleware class, import path string,
                or get_response callable (when called during chain building)
            **init_kwargs: Additional kwargs passed to middleware __init__
        """
        if not DJANGO_AVAILABLE:
            raise ImportError("Django is required to use DjangoMiddleware. Install Django with: pip install django")

        # Check if this is chain building call (get_response is a callable)
        # vs initial configuration (middleware_class is a type or string)
        is_get_response_callable = callable(middleware_class_or_get_response) and not isinstance(
            middleware_class_or_get_response, (type, str)
        )
        if is_get_response_callable:
            raise TypeError(
                "DjangoMiddleware must be configured with a middleware class before being used in a chain. "
                "Use DjangoMiddleware(SomeMiddlewareClass) to create a wrapper."
            )

        # Store middleware class for later instantiation
        if isinstance(middleware_class_or_get_response, str):
            self.middleware_class = import_string(middleware_class_or_get_response)
        else:
            self.middleware_class = middleware_class_or_get_response

        self.init_kwargs = init_kwargs
        self.get_response = None
        self._middleware_instance = None
        self._middleware_is_async = None

    def _create_middleware_instance(self, get_response: Callable) -> None:
        """
        Create the wrapped Django middleware instance.

        Called during chain building when get_response is available.

        Key insight: Django's MiddlewareMixin (used by most middleware) detects
        whether get_response is async and adapts accordingly. By providing an
        async get_response, we enable the middleware to run in async mode,
        avoiding the need for sync_to_async/async_to_sync bridging.
        """
        self.get_response = get_response

        # Create an ASYNC get_response bridge that converts between Bolt and Django
        # This allows Django middleware using MiddlewareMixin to run in async mode
        async def get_response_bridge(django_request: HttpRequest) -> HttpResponse:
            """
            Async get_response for Django middleware.

            Django's MiddlewareMixin detects this is async and uses __acall__,
            which simply awaits get_response - no thread pool overhead.
            """
            try:
                ctx = _request_context.get()
            except LookupError as e:
                raise RuntimeError(
                    "Request context not set. This usually means the middleware chain "
                    "was not properly initialized or a request is being processed outside "
                    "the normal request flow."
                ) from e

            bolt_request = ctx["bolt_request"]

            # Await the async get_response directly - no bridging needed
            bolt_resp = await self.get_response(bolt_request)

            ctx["bolt_response"] = bolt_resp
            _sync_request_attributes(django_request, bolt_request)
            return _to_django_response(bolt_resp)

        # Mark the bridge as a coroutine function so Django's MiddlewareMixin
        # detects it as async and enables async_mode
        markcoroutinefunction(get_response_bridge)

        # Create middleware instance with the async bridge
        self._middleware_instance = self.middleware_class(get_response_bridge, **self.init_kwargs)

        # Check if the middleware instance is async-capable
        # MiddlewareMixin sets this when get_response is async
        # NOTE: We no longer check for "old-style" (process_request/process_response)
        # because MiddlewareMixin already handles these methods correctly in __acall__
        # by wrapping them in sync_to_async. Doing it ourselves causes double-wrapping
        # and severe performance degradation.
        self._middleware_is_async = iscoroutinefunction(self._middleware_instance)

    async def __call__(self, request: Request) -> Response:
        """
        Process request through the Django middleware.

        Follows Django's middleware pattern where __call__(request) processes
        the request and returns a response.
        """
        if self._middleware_instance is None:
            raise RuntimeError(
                "DjangoMiddleware was not properly initialized. "
                "The middleware chain must be built before processing requests."
            )

        # Check if we already have a Django request in context (from outer middleware)
        # This ensures session, user, etc. set by outer middleware are preserved
        existing_ctx = None
        with contextlib.suppress(LookupError):
            existing_ctx = _request_context.get()

        if existing_ctx and "django_request" in existing_ctx:
            # Reuse existing Django request (preserves session, user, etc.)
            django_request = existing_ctx["django_request"]
            token = None
        else:
            # First Django middleware in chain - create new Django request
            django_request = _to_django_request(request)

            # Set up per-request context for the get_response bridge
            ctx = {
                "bolt_request": request,
                "bolt_response": None,
                "django_request": django_request,
            }
            token = _request_context.set(ctx)

        try:
            if self._middleware_is_async:
                # Async-capable middleware (e.g., using MiddlewareMixin with async get_response)
                # MiddlewareMixin.__acall__ handles process_request/process_response internally
                # by wrapping them in sync_to_async - we don't need to do it ourselves
                django_response = await self._middleware_instance(django_request)
            else:
                # Sync middleware without async support - run in thread pool
                django_response = await sync_to_async(self._middleware_instance, thread_sensitive=True)(django_request)
            return _to_bolt_response(django_response)
        finally:
            if token is not None:
                _request_context.reset(token)

    def __repr__(self) -> str:
        return f"DjangoMiddleware({self.middleware_class.__name__})"


# No-op get_response for hook-based middleware instances
# Hook-based middleware only use process_request/process_response, never call get_response
def _noop_get_response(request):
    """Placeholder get_response for hook-based middleware that never gets called."""
    raise RuntimeError("Hook-based middleware should not call get_response")


# Pre-created CSRF callback singletons to avoid function creation on hot path
# Django's CsrfViewMiddleware checks getattr(callback, "csrf_exempt", False)
def _csrf_callback_not_exempt(request):
    pass


def _csrf_callback_exempt(request):
    pass


_csrf_callback_exempt.csrf_exempt = True
_csrf_callback_not_exempt.csrf_exempt = False


# Module-level constants to avoid allocation on hot path
_EMPTY_TUPLE: tuple = ()
_EMPTY_DICT: dict = {}

# Frozenset for O(1) header skip check (avoid tuple creation in loop)
_SKIP_HEADERS = frozenset(("content-type", "content-length"))


# Known-safe Django middleware modules that don't do blocking I/O in hooks
# These get the fast path (direct calls without sync_to_async)
# NOTE: django.contrib.sessions, django.contrib.auth, and django.contrib.messages
# are NOT included here because they can perform blocking database I/O
# (e.g., SessionMiddleware.process_response saves session to DB)
_DJANGO_SAFE_MIDDLEWARE_PREFIXES = frozenset(
    [
        "django.middleware.",  # security, common, csrf, clickjacking, gzip, locale, http
        "django.contrib.flatpages.",  # FlatpageFallbackMiddleware
        "django.contrib.redirects.",  # RedirectFallbackMiddleware
        "django.contrib.sites.",  # CurrentSiteMiddleware
        "django.contrib.admindocs.",  # XViewMiddleware
    ]
)


def _is_django_builtin_middleware(middleware_class: type) -> bool:
    """Check if middleware is a known-safe Django built-in.

    Django's built-in middleware hooks (process_request/process_response) are
    designed to be fast and non-blocking. They only do:
    - Set attributes (request.session, request.user)
    - Check headers
    - Manipulate cookies

    Third-party middleware might do blocking I/O (database queries, HTTP calls)
    in their hooks, so we need to use sync_to_async for safety.
    """
    module = middleware_class.__module__
    return any(module.startswith(prefix) for prefix in _DJANGO_SAFE_MIDDLEWARE_PREFIXES)


class DjangoMiddlewareStack:
    """
    Wraps MULTIPLE Django middleware classes into a SINGLE Bolt middleware.

    HYBRID OPTIMIZATION with SAFETY:
    Middleware is categorized into three groups:

    1. **Django built-in hook-based middleware**:
       Called DIRECTLY without any thread pool overhead. This is the fast path.
       Safe because Django's built-in hooks don't do blocking I/O.

    2. **Third-party hook-based middleware**:
       Wrapped in sync_to_async(thread_sensitive=True) for safety.
       Third-party middleware might do database queries or HTTP calls in hooks.

    3. **__call__-only middleware** (only overrides __call__):
       Wrapped in a sync chain and executed via ONE sync_to_async call.
       This is slower but necessary for middleware that needs to wrap get_response.

    Performance impact:
    - Django built-in hooks: ~0.007ms (direct calls)
    - Third-party hooks: ~0.8ms (sync_to_async overhead)
    - With __call__-only: additional ~0.5ms per middleware

    Usage:
        api = BoltAPI(django_middleware=True)
    """

    __slots__ = (
        "middleware_classes",
        "get_response",
        "_django_hook_middleware",  # Django built-in: fast path (direct calls)
        "_thirdparty_hook_middleware",  # Third-party: safe path (sync_to_async)
        "_call_middleware_chain",  # __call__-only middleware chain (or None)
        "_call_middleware_chain_async",  # Precomputed sync_to_async wrapper
        "_compatibility_chain",  # Correctness-first mixed hook/call-only path
        "_ordered_hook_middleware",  # Hook middleware in declared order
        # Pre-computed for hot path (avoid hasattr/reversed in loops)
        "_django_process_request",  # Middleware with process_request
        "_django_process_response_reversed",  # Middleware with process_response (reversed)
        "_django_process_view",  # Middleware with process_view
        "_thirdparty_process_request",  # Third-party with process_request
        "_thirdparty_process_response_reversed",  # Third-party with process_response (reversed)
        "_thirdparty_process_view",  # Third-party with process_view
    )

    def __init__(self, middleware_classes: list):
        """
        Initialize the Django middleware stack.

        Args:
            middleware_classes: List of Django middleware classes (not instances)
                               in the order they should be applied (outermost first)
        """
        if not DJANGO_AVAILABLE:
            raise ImportError(
                "Django is required to use DjangoMiddlewareStack. Install Django with: pip install django"
            )

        self.middleware_classes = middleware_classes
        self.get_response = None
        self._django_hook_middleware = []  # Django built-in: direct calls
        self._thirdparty_hook_middleware = []  # Third-party: sync_to_async
        self._call_middleware_chain = None  # __call__-only: sync chain
        self._call_middleware_chain_async = None
        self._compatibility_chain = None
        self._ordered_hook_middleware = []
        # Pre-computed lists (populated in _create_middleware_instance)
        self._django_process_request = []
        self._django_process_response_reversed = []
        self._django_process_view = []
        self._thirdparty_process_request = []
        self._thirdparty_process_response_reversed = []
        self._thirdparty_process_view = []

    @staticmethod
    def _has_hook_methods(middleware_class: type) -> bool:
        """Return True if middleware defines any Django hook methods."""
        return any(
            hasattr(middleware_class, method_name)
            for method_name in ("process_request", "process_view", "process_response")
        )

    @staticmethod
    def _prepare_hook_method(
        instance: Any,
        method_name: str,
        *,
        direct: bool,
    ) -> tuple[Callable | None, bool]:
        """Return (callable, is_async_callable) for a hook method."""
        hook_method = getattr(instance, method_name, None)
        if hook_method is None:
            return None, False
        if direct:
            return hook_method, False
        return sync_to_async(hook_method, thread_sensitive=True), True

    def _create_hook_entry(self, middleware_class: type) -> dict[str, Any]:
        """Create a middleware hook entry with precomputed wrappers."""
        is_django_builtin = _is_django_builtin_middleware(middleware_class)
        instance = middleware_class(_noop_get_response)

        process_request, process_request_is_async = self._prepare_hook_method(
            instance,
            "process_request",
            direct=is_django_builtin,
        )
        process_view, process_view_is_async = self._prepare_hook_method(
            instance,
            "process_view",
            direct=is_django_builtin,
        )
        process_response, process_response_is_async = self._prepare_hook_method(
            instance,
            "process_response",
            direct=is_django_builtin,
        )

        return {
            "instance": instance,
            "is_django_builtin": is_django_builtin,
            "process_request": process_request,
            "process_request_is_async": process_request_is_async,
            "process_view": process_view,
            "process_view_is_async": process_view_is_async,
            "process_response": process_response,
            "process_response_is_async": process_response_is_async,
        }

    async def _invoke_hook(self, hook_callable: Callable | None, is_async_callable: bool, *args: Any) -> Any:
        """Invoke a precomputed hook callable."""
        if hook_callable is None:
            return None
        if is_async_callable:
            return await hook_callable(*args)
        return hook_callable(*args)

    def _create_middleware_instance(self, get_response: Callable) -> None:
        """
        Create middleware instances, separating into three categories:
        1. Django built-in hook-based -> direct calls (fast, safe)
        2. Third-party hook-based -> sync_to_async (slower, but safe for blocking I/O)
        3. __call__-only -> sync chain with sync_to_async
        """
        self.get_response = get_response
        self._django_hook_middleware = []
        self._thirdparty_hook_middleware = []
        self._call_middleware_chain = None
        self._call_middleware_chain_async = None
        self._compatibility_chain = None
        self._ordered_hook_middleware = []
        self._django_process_request = []
        self._django_process_response_reversed = []
        self._django_process_view = []
        self._thirdparty_process_request = []
        self._thirdparty_process_response_reversed = []
        self._thirdparty_process_view = []

        call_only_classes = []

        for middleware_class in self.middleware_classes:
            if self._has_hook_methods(middleware_class):
                entry = self._create_hook_entry(middleware_class)
                self._ordered_hook_middleware.append(entry)

                if entry["is_django_builtin"]:
                    self._django_hook_middleware.append(entry["instance"])
                else:
                    self._thirdparty_hook_middleware.append(entry["instance"])
            else:
                call_only_classes.append(middleware_class)

        # Keep legacy categorized lists for introspection/tests.
        self._django_process_request = [
            instance for instance in self._django_hook_middleware if hasattr(instance, "process_request")
        ]
        self._django_process_view = [
            instance for instance in self._django_hook_middleware if hasattr(instance, "process_view")
        ]
        self._django_process_response_reversed = list(
            reversed([instance for instance in self._django_hook_middleware if hasattr(instance, "process_response")])
        )
        self._thirdparty_process_request = [
            instance for instance in self._thirdparty_hook_middleware if hasattr(instance, "process_request")
        ]
        self._thirdparty_process_view = [
            instance for instance in self._thirdparty_hook_middleware if hasattr(instance, "process_view")
        ]
        self._thirdparty_process_response_reversed = list(
            reversed(
                [instance for instance in self._thirdparty_hook_middleware if hasattr(instance, "process_response")]
            )
        )

        has_hook_middleware = bool(self._ordered_hook_middleware)
        has_call_only_middleware = bool(call_only_classes)

        # Compatibility fallback: mixed hook + __call__ middleware cannot be safely flattened
        # while preserving strict declared order and process_view semantics.
        if has_hook_middleware and has_call_only_middleware:
            self._compatibility_chain = self._build_compatibility_chain()
            return

        # __call__-only stack: keep one sync_to_async boundary, precomputed at build time.
        if has_call_only_middleware:
            self._call_middleware_chain = self._build_call_only_chain(call_only_classes)
            self._call_middleware_chain_async = sync_to_async(self._call_middleware_chain, thread_sensitive=True)

    def _build_call_only_chain(self, call_only_classes: list) -> Callable:
        """Build the sync middleware chain for __call__-only middleware."""

        def innermost_sync_handler(django_request):
            """Sync bridge that calls async handler via async_to_sync."""
            ctx = _request_context.get()
            bolt_request = ctx["bolt_request"]

            _sync_request_attributes(django_request, bolt_request)
            bolt_resp = async_to_sync(self.get_response)(bolt_request)
            ctx["bolt_response"] = bolt_resp

            return _to_django_response(bolt_resp)

        chain = innermost_sync_handler
        for middleware_class in reversed(call_only_classes):
            chain = middleware_class(chain)

        return chain

    def _build_compatibility_chain(self) -> Callable:
        """Build correctness-first chain for mixed hook + __call__ middleware."""

        async def terminal(django_request):
            ctx = _request_context.get()
            bolt_request = ctx["bolt_request"]
            entered_hook_entries = ctx["entered_hook_entries"]

            _sync_request_attributes(django_request, bolt_request)

            csrf_exempt = bolt_request.state.get("_csrf_exempt", False) if bolt_request.state else False
            csrf_callback = _csrf_callback_exempt if csrf_exempt else _csrf_callback_not_exempt

            for entry in entered_hook_entries:
                if entry["process_view"] is None:
                    continue
                response = await self._invoke_hook(
                    entry["process_view"],
                    entry["process_view_is_async"],
                    django_request,
                    csrf_callback,
                    _EMPTY_TUPLE,
                    _EMPTY_DICT,
                )
                if response is not None:
                    return response

            bolt_response = await self.get_response(bolt_request)
            return _to_django_response(bolt_response)

        chain = terminal

        for middleware_class in reversed(self.middleware_classes):
            if self._has_hook_methods(middleware_class):
                entry = self._create_hook_entry(middleware_class)
                next_layer = chain

                async def hook_layer(django_request, *, _entry=entry, _next=next_layer):
                    ctx = _request_context.get()
                    entered_hook_entries = ctx["entered_hook_entries"]
                    entered_hook_entries.append(_entry)
                    try:
                        response = None
                        if _entry["process_request"] is not None:
                            response = await self._invoke_hook(
                                _entry["process_request"],
                                _entry["process_request_is_async"],
                                django_request,
                            )
                        if response is None:
                            response = await _next(django_request)
                    finally:
                        entered_hook_entries.pop()

                    if _entry["process_response"] is not None:
                        response = await self._invoke_hook(
                            _entry["process_response"],
                            _entry["process_response_is_async"],
                            django_request,
                            response,
                        )
                    return response

                chain = hook_layer
                continue

            next_layer = chain

            def get_response_sync(django_request, _next=next_layer):
                return async_to_sync(_next)(django_request)

            instance = middleware_class(get_response_sync)
            if iscoroutinefunction(instance):

                async def call_layer(django_request, *, _instance=instance):
                    return await _instance(django_request)

            else:
                call_layer_async = sync_to_async(instance, thread_sensitive=True)

                async def call_layer(django_request, *, _call_layer=call_layer_async):
                    return await _call_layer(django_request)

            chain = call_layer

        return chain

    async def __call__(self, request: Request) -> Response:
        """
        Process request through the Django middleware stack.

        HYBRID APPROACH WITH SAFETY:
        1. Convert Bolt request to Django request ONCE
        2. Run Django built-in process_request hooks DIRECTLY (fast, safe!)
        3. Run third-party process_request hooks via sync_to_async (safe for blocking I/O)
        4. Run process_view hooks (for CSRF validation, etc.)
        5. Either:
           a. If no __call__-only middleware: await handler directly (fast!)
           b. If __call__-only middleware: run chain via sync_to_async (slow but necessary)
        6. Run third-party process_response hooks via sync_to_async (reverse order)
        7. Run Django built-in process_response hooks DIRECTLY (reverse order)
        8. Convert Django response to Bolt response ONCE
        """
        # 1. Single Bolt→Django conversion
        django_request = _to_django_request(request)

        # Mixed hook + call-only path: strict-order compatibility mode.
        if self._compatibility_chain is not None:
            ctx = {
                "bolt_request": request,
                "bolt_response": None,
                "django_request": django_request,
                "entered_hook_entries": [],
            }
            token = _request_context.set(ctx)
            try:
                django_response = await self._compatibility_chain(django_request)
            finally:
                _request_context.reset(token)
            return _to_bolt_response(django_response)

        # Hook-only optimized path with strict declared ordering.
        if self._ordered_hook_middleware:
            entered_hook_entries = []
            django_response = None

            # Run process_request hooks in declared order.
            for entry in self._ordered_hook_middleware:
                entered_hook_entries.append(entry)
                if entry["process_request"] is None:
                    continue
                response = await self._invoke_hook(
                    entry["process_request"],
                    entry["process_request_is_async"],
                    django_request,
                )
                if response is not None:
                    django_response = response
                    break

            # No request short-circuit: run process_view hooks, then handler.
            if django_response is None:
                _sync_request_attributes(django_request, request)

                csrf_exempt = request.state.get("_csrf_exempt", False) if request.state else False
                csrf_callback = _csrf_callback_exempt if csrf_exempt else _csrf_callback_not_exempt

                for entry in entered_hook_entries:
                    if entry["process_view"] is None:
                        continue
                    response = await self._invoke_hook(
                        entry["process_view"],
                        entry["process_view_is_async"],
                        django_request,
                        csrf_callback,
                        _EMPTY_TUPLE,
                        _EMPTY_DICT,
                    )
                    if response is not None:
                        django_response = response
                        break

                if django_response is None:
                    bolt_response = await self.get_response(request)
                    django_response = _to_django_response(bolt_response)

            # Apply process_response hooks in reverse order for all entered middleware.
            for entry in reversed(entered_hook_entries):
                if entry["process_response"] is None:
                    continue
                django_response = await self._invoke_hook(
                    entry["process_response"],
                    entry["process_response_is_async"],
                    django_request,
                    django_response,
                )

            return _to_bolt_response(django_response)

        # __call__-only path (no hook middleware).
        if self._call_middleware_chain_async is not None:
            ctx = {
                "bolt_request": request,
                "bolt_response": None,
                "django_request": django_request,
            }
            token = _request_context.set(ctx)
            try:
                django_response = await self._call_middleware_chain_async(django_request)
            finally:
                _request_context.reset(token)
            return _to_bolt_response(django_response)

        # No middleware classes configured in the stack.
        return await self.get_response(request)

    def __repr__(self) -> str:
        names = [cls.__name__ for cls in self.middleware_classes]
        return f"DjangoMiddlewareStack([{', '.join(names)}])"


# ============================================================================
# Module-level helper functions (shared by DjangoMiddleware and DjangoMiddlewareStack)
# ============================================================================


def _to_django_request(request: Request) -> HttpRequest:
    """Convert Bolt Request to Django HttpRequest.

    Performance optimizations:
    - Reuse empty dicts/QueryDicts where possible
    - Skip BytesIO creation for empty bodies
    - Use direct attribute assignment (faster than setattr)
    """
    django_request = HttpRequest()

    # Copy basic attributes (direct assignment is faster)
    django_request.method = request.method
    django_request.path = request.path
    django_request.path_info = request.path

    # Build META dict from headers
    django_request.META = _build_meta(request)

    # Copy cookies - use empty dict directly if no cookies
    # Note: When django_middleware is enabled, needs_cookies=True is set at registration
    # time, ensuring Rust always parses and passes cookies to Python
    django_request.COOKIES = dict(request.cookies) if request.cookies else {}

    # Query params - only create mutable QueryDict if we have params
    if request.query:
        django_request.GET = QueryDict(mutable=True)
        for key, value in request.query.items():
            django_request.GET[key] = value
    else:
        django_request.GET = _get_empty_querydict()  # Reuse singleton (no allocation)

    # Parse POST data for form submissions (needed for CSRF token validation)
    # Django's CsrfViewMiddleware reads request.POST['csrfmiddlewaretoken']
    content_type = request.headers.get("content-type", "")
    body = request.body if request.body else b""

    if body and "application/x-www-form-urlencoded" in content_type:
        # Parse form data into POST QueryDict
        django_request.POST = QueryDict(body, mutable=False)
    else:
        django_request.POST = _get_empty_querydict()  # Reuse singleton (no allocation)

    # Store body for raw access
    django_request._body = body
    if body:
        django_request._stream = io.BytesIO(body)

    # Store reference to Bolt request for attribute sync
    django_request._bolt_request = request

    return django_request


def _build_meta(request: Request) -> dict:
    """Build Django META dict from Bolt request headers."""
    query_string = "&".join(f"{k}={v}" for k, v in request.query.items()) if request.query else ""

    meta = {
        "REQUEST_METHOD": request.method,
        "PATH_INFO": request.path,
        "QUERY_STRING": query_string,
        "CONTENT_TYPE": request.headers.get("content-type", ""),
        "CONTENT_LENGTH": str(len(request.body)) if request.body else "",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8000",
    }

    # Convert headers to META format
    for key, value in request.headers.items():
        key_lower = key.lower()
        # Skip content-type and content-length (already added) - O(1) frozenset lookup
        if key_lower in _SKIP_HEADERS:
            continue
        # Convert to Django META format (HTTP_HEADER_NAME)
        meta_key = f"HTTP_{key.upper().replace('-', '_')}"
        meta[meta_key] = value

    return meta


def _sync_request_attributes(django_request: HttpRequest, bolt_request: Request) -> None:
    """
    Sync attributes added by Django middleware to Bolt request.

    Django middlewares commonly add:
    - request.user (AuthenticationMiddleware) - SimpleLazyObject for sync access
    - request.auser (AuthenticationMiddleware) - async callable for async access
    - request.session (SessionMiddleware)
    - request.csrf_processing_done (CsrfViewMiddleware)

    Custom middleware can add arbitrary attributes which are synced to request.state
    since PyRequest is a Rust object with fixed attributes.

    Performance: Uses getattr(obj, attr, None) pattern instead of hasattr() + access
    to avoid double attribute lookup.
    """
    # Sync user (SimpleLazyObject) for sync access - this is a writable attribute on PyRequest
    user = getattr(django_request, "user", None)
    if user is not None:
        bolt_request.user = user

    # Sync auser (async callable) for async access via `await request.state["auser"]()`
    auser = getattr(django_request, "auser", None)
    if auser is not None:
        bolt_request.state["auser"] = auser

    # Sync session to state
    session = getattr(django_request, "session", None)
    if session is not None:
        bolt_request.state["session"] = session

    # Sync _messages for Django's messages framework
    # This enables {% for message in messages %} in templates when using MessageMiddleware
    messages = getattr(django_request, "_messages", None)
    if messages is not None:
        bolt_request.state["_messages"] = messages

    # Sync META for Django template compatibility (e.g., {% csrf_token %})
    # Django's get_token() needs request.META['CSRF_COOKIE']
    # Note: HttpRequest always has META, no need to check - direct access
    bolt_request.state["META"] = django_request.META

    # Sync other common middleware attributes to state (use getattr pattern)
    csrf_processing_done = getattr(django_request, "csrf_processing_done", None)
    if csrf_processing_done is not None:
        bolt_request.state["csrf_processing_done"] = csrf_processing_done

    csrf_cookie_needs_reset = getattr(django_request, "csrf_cookie_needs_reset", None)
    if csrf_cookie_needs_reset is not None:
        bolt_request.state["csrf_cookie_needs_reset"] = csrf_cookie_needs_reset


def _to_django_response(response: Response) -> HttpResponse:
    """Convert Bolt Response/MiddlewareResponse to Django HttpResponse.

    Also handles Django HttpResponse pass-through (from decorators like @login_required).
    """
    # Fast path: if already a Django HttpResponse, return as-is
    if isinstance(response, HttpResponse):
        return response

    # Handle different response types
    if hasattr(response, "body"):
        # MiddlewareResponse has .body
        content = response.body if isinstance(response.body, bytes) else str(response.body).encode()
    elif hasattr(response, "to_bytes"):
        content = response.to_bytes()
    elif hasattr(response, "content"):
        content = response.content if isinstance(response.content, bytes) else str(response.content).encode()
    else:
        content = b""

    status_code = getattr(response, "status_code", 200)
    headers = getattr(response, "headers", {})

    django_response = HttpResponse(
        content=content,
        status=status_code,
        content_type=headers.get("content-type", headers.get("Content-Type", "application/json")),
    )

    for key, value in headers.items():
        if key.lower() != "content-type":
            django_response[key] = value

    return django_response


def _to_bolt_response(django_response: HttpResponse) -> MiddlewareResponse:
    """Convert Django HttpResponse to MiddlewareResponse for chain compatibility."""
    headers = dict(django_response.items())

    # IMPORTANT: Extract cookies from django_response.cookies as raw tuples
    # Django's set_cookie() stores cookies in response.cookies (SimpleCookie),
    # NOT in the regular headers. We extract raw data so Rust can serialize.
    # This is critical for CSRF cookie to be set by CsrfViewMiddleware.process_response
    raw_cookies = None
    if hasattr(django_response, "cookies") and django_response.cookies:
        raw_cookies = []
        for morsel in django_response.cookies.values():
            # Extract raw data from Morsel object for Rust serialization
            # Cookie tuple: (name, value, path, max_age, expires, domain, secure, httponly, samesite)
            max_age = morsel.get("max-age")
            raw_cookies.append(
                (
                    morsel.key,  # name
                    morsel.value,  # value
                    morsel.get("path") or "/",  # path
                    int(max_age) if max_age else None,  # max_age
                    morsel.get("expires") or None,  # expires
                    morsel.get("domain") or None,  # domain
                    bool(morsel.get("secure")),  # secure
                    bool(morsel.get("httponly")),  # httponly
                    morsel.get("samesite") or None,  # samesite
                )
            )

    return MiddlewareResponse(
        status_code=django_response.status_code,
        headers=headers,
        body=django_response.content,
        raw_cookies=raw_cookies,
    )


__all__ = ["DjangoMiddleware", "DjangoMiddlewareStack"]
