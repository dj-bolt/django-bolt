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
import functools
import io
from collections.abc import Callable
from inspect import iscoroutine
from typing import TYPE_CHECKING, Any

from .._core import RequestLane
from ..concurrency import drive_on_lane, in_lane_mode
from ..middleware_response import (
    _BODY_BYTES,
    _BODY_FILE,
    _BODY_STREAM,
    MiddlewareResponse,
    _split_content_type_headers,
)

try:
    from asgiref.sync import (
        SyncToAsync,
        ThreadSensitiveContext,
        async_to_sync,
        iscoroutinefunction,
        markcoroutinefunction,
        sync_to_async,
    )
    from django.http import HttpRequest, HttpResponse, QueryDict
    from django.utils.deprecation import MiddlewareMixin
    from django.utils.functional import LazyObject, empty
    from django.utils.module_loading import import_string

    DJANGO_AVAILABLE = True
except ImportError:
    DJANGO_AVAILABLE = False
    async_to_sync = None
    ThreadSensitiveContext = None
    SyncToAsync = None
    HttpRequest = None
    HttpResponse = None
    QueryDict = None
    import_string = None
    MiddlewareMixin = None
    sync_to_async = None
    iscoroutinefunction = None
    markcoroutinefunction = None
    LazyObject = None
    empty = None

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

_PRESERVED_BODY_ATTR = "_bolt_preserved_body"
_PRESERVED_BODY_KIND_ATTR = "_bolt_preserved_body_kind"
_PRESERVED_BODY_KINDS = frozenset((_BODY_FILE, _BODY_STREAM))
_REBUILT_BODY_HEADER_NAMES = frozenset(("content-length", "transfer-encoding"))
_DEFAULT_DJANGO_RESPONSE_CONTENT_TYPE = "application/json"
# Attributes that HttpRequest.__init__ sets, and those that Bolt copies by name.
# All other public attributes come from middleware and go to request.state.
# This is a literal set: HttpRequest() needs configured settings, and
# nanodjango imports this module before it configures them.
_STANDARD_REQUEST_ATTRS = frozenset(
    (
        "GET",
        "POST",
        "COOKIES",
        "META",
        "FILES",
        "path",
        "path_info",
        "method",
        "resolver_match",
        "content_type",
        "content_params",
        # cached_property values land in __dict__ after the first read.
        "headers",
        "accepted_types",
        "accepted_types_by_precedence",
        "user",
        "auser",
        "session",
        "csrf_processing_done",
        "csrf_cookie_needs_reset",
    )
)


async def _run_request_affine(call: Callable, request: Request) -> Response:
    """Run ``call`` with one request-owned thread for its sync work."""
    # This is the boundary of Django's ASGI handler (ThreadSensitiveContext).
    # The request takes a Rust lane at its first sync call (see lane.rs).
    # A lane or an outer Django wrapper that owns the thread gets no nested context.
    if in_lane_mode() or SyncToAsync.thread_sensitive_context.get(None) is not None:
        return await call(request)
    context = ThreadSensitiveContext()
    lane = RequestLane()
    SyncToAsync.context_to_thread_executor[context] = lane
    token = SyncToAsync.thread_sensitive_context.set(context)
    try:
        return await call(request)
    finally:
        SyncToAsync.thread_sensitive_context.reset(token)
        del SyncToAsync.context_to_thread_executor[context]
        lane.release()


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
        "_sync_call_override",
        "_lane_instance",
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
        self._sync_call_override = False
        self._lane_instance = None

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

            # Copy the middleware attributes before the handler reads them.
            _sync_request_attributes(django_request, bolt_request)

            # Await the async get_response directly - no bridging needed
            bolt_resp = await self.get_response(bolt_request)

            ctx["bolt_response"] = bolt_resp
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
        # MiddlewareMixin marks each instance as async. A subclass with its own
        # sync ``__call__`` must run on the thread of the request. Its result is
        # a response, or the coroutine of ``super().__call__``.
        # ``middleware_class`` can be a factory, so look at the instance that it returns.
        instance_call = type(self._middleware_instance).__call__
        self._sync_call_override = (
            isinstance(self._middleware_instance, MiddlewareMixin)
            and instance_call is not MiddlewareMixin.__call__
            and not iscoroutinefunction(instance_call)
        )

        if self.supports_lane_dispatch:
            # A lane has no event loop. It uses a second instance in sync mode,
            # which is the mode that Django uses under WSGI.
            def lane_get_response(django_request: HttpRequest) -> HttpResponse:
                ctx = _request_context.get()
                bolt_request = ctx["bolt_request"]
                _sync_request_attributes(django_request, bolt_request)
                bolt_resp = drive_on_lane(self.get_response(bolt_request))
                ctx["bolt_response"] = bolt_resp
                return _to_django_response(bolt_resp)

            self._lane_instance = self.middleware_class(lane_get_response, **self.init_kwargs)

    @property
    def supports_lane_dispatch(self) -> bool:
        """Whether a lane can run this middleware in sync mode, with no event loop."""
        middleware_class = self.middleware_class
        return (
            isinstance(middleware_class, type)
            and getattr(middleware_class, "sync_capable", True)
            and not iscoroutinefunction(middleware_class.__call__)
            # A subclass can put logic in ``__acall__``, which sync mode does not run.
            and getattr(middleware_class, "__acall__", MiddlewareMixin.__acall__) is MiddlewareMixin.__acall__
        )

    async def __call__(self, request: Request) -> Response:
        """Run the Django middleware in one request-owned sync context."""
        return await _run_request_affine(self._call, request)

    async def _call(self, request: Request) -> Response:
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
            if in_lane_mode():
                django_response = self._lane_instance(django_request)
            elif self._sync_call_override:
                # The bound method has no coroutine mark, where the instance has one.
                django_response = await sync_to_async(self._middleware_instance.__call__, thread_sensitive=True)(
                    django_request
                )
                if iscoroutine(django_response):
                    django_response = await django_response
            elif self._middleware_is_async:
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
        "_request_phase_async",  # One thread hop for all request-phase hooks
        "_response_phase_async",  # One thread hop for all response-phase hooks
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
        self._request_phase_async = None
        self._response_phase_async = None
        self._compatibility_chain = None
        self._ordered_hook_middleware = []
        # Pre-computed lists (populated in _create_middleware_instance)
        self._django_process_request = []
        self._django_process_response_reversed = []
        self._django_process_view = []
        self._thirdparty_process_request = []
        self._thirdparty_process_response_reversed = []
        self._thirdparty_process_view = []

    @property
    def supports_lane_dispatch(self) -> bool:
        """Whether a lane can run this stack with no event loop.

        A middleware with an async ``__call__`` can await, so it needs the event
        loop. ``sync_capable`` is the flag that Django reads for a class or a factory.
        """
        return not any(
            not getattr(middleware, "sync_capable", True)
            or (isinstance(middleware, type) and iscoroutinefunction(middleware.__call__))
            for middleware in self.middleware_classes
        )

    @staticmethod
    def _has_hook_methods(middleware_class: type) -> bool:
        """Return True if middleware defines any Django hook methods."""
        return any(
            hasattr(middleware_class, method_name)
            for method_name in ("process_request", "process_view", "process_response")
        )

    def _create_hook_entry(self, middleware_class: type) -> dict[str, Any]:
        """Create a middleware hook entry with precomputed wrappers."""
        is_django_builtin = _is_django_builtin_middleware(middleware_class)
        instance = middleware_class(_noop_get_response)
        entry = {"instance": instance, "is_django_builtin": is_django_builtin}
        for name in ("process_request", "process_view", "process_response"):
            raw = getattr(instance, name, None)
            # The plain sync hook. One thread hop runs all of them in order.
            entry[f"raw_{name}"] = raw
            # The compatibility chain calls hook by hook. A third-party hook can
            # block, so there it runs on the thread of the request.
            if raw is None or is_django_builtin:
                entry[name] = raw
            else:
                entry[name] = sync_to_async(raw, thread_sensitive=True)
        return entry

    async def _invoke_hook(self, raw: Callable | None, hook: Callable | None, *args: Any) -> Any:
        """Call one hook of the compatibility chain. ``hook`` is ``raw`` or its thread hop."""
        if raw is None:
            return None
        if hook is raw or in_lane_mode():
            return raw(*args)
        return await hook(*args)

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
        self._request_phase_async = None
        self._response_phase_async = None
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

        # A third-party hook can block, so it needs the request's thread. One hop
        # then runs all hooks of a phase, the Django built-in ones too.
        if self._thirdparty_hook_middleware:
            entries = self._ordered_hook_middleware
            if any(e["raw_process_request"] or e["raw_process_view"] for e in entries):
                self._request_phase_async = sync_to_async(self._run_request_phase, thread_sensitive=True)
            if any(e["raw_process_response"] for e in entries):
                self._response_phase_async = sync_to_async(self._run_response_phase, thread_sensitive=True)

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

    def _run_request_phase(self, django_request: HttpRequest, request: Request) -> tuple[HttpResponse | None, int]:
        """Run process_request and process_view of all hooks, in declared order.

        Return the short-circuit response, if one exists, and the count of
        middleware that the request entered.
        """
        entries = self._ordered_hook_middleware
        entered = 0
        for entry in entries:
            entered += 1
            hook = entry["raw_process_request"]
            if hook is not None:
                response = hook(django_request)
                if response is not None:
                    return response, entered

        _sync_request_attributes(django_request, request)
        csrf_exempt = request.state.get("_csrf_exempt", False) if request.state else False
        csrf_callback = _csrf_callback_exempt if csrf_exempt else _csrf_callback_not_exempt
        for entry in entries:
            hook = entry["raw_process_view"]
            if hook is not None:
                response = hook(django_request, csrf_callback, _EMPTY_TUPLE, _EMPTY_DICT)
                if response is not None:
                    return response, entered
        return None, entered

    def _run_response_phase(self, django_request: HttpRequest, django_response: HttpResponse, entered: int):
        """Run process_response of each entered middleware, in reverse order."""
        entries = self._ordered_hook_middleware
        for index in range(entered - 1, -1, -1):
            hook = entries[index]["raw_process_response"]
            if hook is not None:
                django_response = hook(django_request, django_response)
        return django_response

    def _build_call_only_chain(self, call_only_classes: list) -> Callable:
        """Build the sync middleware chain for __call__-only middleware."""

        def innermost_sync_handler(django_request):
            """Sync bridge that calls async handler via async_to_sync."""
            ctx = _request_context.get()
            bolt_request = ctx["bolt_request"]

            _sync_request_attributes(django_request, bolt_request)
            if in_lane_mode():
                bolt_resp = drive_on_lane(self.get_response(bolt_request))
            else:
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
                    entry["raw_process_view"],
                    entry["process_view"],
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
                                _entry["raw_process_request"],
                                _entry["process_request"],
                                django_request,
                            )
                        if response is None:
                            response = await _next(django_request)
                    finally:
                        entered_hook_entries.pop()

                    if _entry["process_response"] is not None:
                        response = await self._invoke_hook(
                            _entry["raw_process_response"],
                            _entry["process_response"],
                            django_request,
                            response,
                        )
                    return response

                chain = hook_layer
                continue

            next_layer = chain

            def get_response_sync(django_request, _next=next_layer):
                if in_lane_mode():
                    return drive_on_lane(_next(django_request))
                return async_to_sync(_next)(django_request)

            instance = middleware_class(get_response_sync)
            if iscoroutinefunction(instance):

                async def call_layer(django_request, *, _instance=instance):
                    return await _instance(django_request)

            else:
                call_layer_async = sync_to_async(instance, thread_sensitive=True)

                async def call_layer(django_request, *, _call_layer=call_layer_async):
                    if in_lane_mode():
                        return _call_layer.func(django_request)
                    return await _call_layer(django_request)

            chain = call_layer

        return chain

    async def __call__(self, request: Request) -> Response:
        """Run the complete Django stack in one request-owned sync context."""
        return await _run_request_affine(self._call, request)

    async def _call(self, request: Request) -> Response:
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

        # Hook-only path. A phase with a third-party hook uses one thread hop,
        # because that hook can block. All other phases run direct.
        if self._ordered_hook_middleware:
            inline = in_lane_mode()
            if inline or self._request_phase_async is None:
                django_response, entered = self._run_request_phase(django_request, request)
            else:
                django_response, entered = await self._request_phase_async(django_request, request)
            if django_response is None:
                django_response = _to_django_response(await self.get_response(request))
            if inline or self._response_phase_async is None:
                django_response = self._run_response_phase(django_request, django_response, entered)
            else:
                django_response = await self._response_phase_async(django_request, django_response, entered)
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
                if in_lane_mode():
                    django_response = self._call_middleware_chain(django_request)
                else:
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

    # One META dict per request. Rust builds it once (REMOTE_ADDR from the
    # client-ip resolver, SERVER_NAME from Host, HTTP_* headers) and caches it,
    # so middleware writes (CSRF_COOKIE) are visible to the handler's request.META.
    django_request.META = request.META

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


def _should_adopt_django_user(django_user: Any, bolt_request: Request) -> bool:
    """
    Decide whether a user set on the Django request should replace
    bolt_request.user.

    Bolt route-level auth (JWT/API key, validated in Rust) attaches a lazy
    user in _dispatch BEFORE the Django middleware chain runs. Django's
    AuthenticationMiddleware then unconditionally sets its own session-based
    user — AnonymousUser for requests authenticated only via Bolt auth —
    which must not clobber the Bolt user (the backend's get_user would then
    never run).

    Rules:
    - No Bolt user set → adopt Django's user (plain Django behavior).
    - Django user is AuthenticationMiddleware's still-unevaluated lazy
      default → keep the Bolt user (also avoids forcing a session query).
    - Django user was evaluated or explicitly assigned (login(),
      impersonation middleware) → adopt it only if it is actually
      authenticated; an anonymous result never overrides Bolt auth.
    """
    if bolt_request.user is None:
        return True
    if isinstance(django_user, LazyObject) and django_user._wrapped is empty:
        return False
    return bool(getattr(django_user, "is_authenticated", False))


async def _static_auser(user):
    """Module-level `auser` replacement bound via functools.partial — avoids
    allocating a fresh coroutine function per request."""
    return user


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
    auser = getattr(django_request, "auser", None)
    if user is not None:
        if _should_adopt_django_user(user, bolt_request):
            bolt_request.user = user
            # Sync auser (async callable) for async access via `await request.auser()`
            if auser is not None:
                bolt_request.state["auser"] = auser
        else:
            # Keep the Bolt-auth user and make auser consistent with it,
            # instead of Django's session-based (anonymous) auser.
            bolt_request.state["auser"] = functools.partial(_static_auser, bolt_request.user)
    elif auser is not None:
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

    # Sync other common middleware attributes to state (use getattr pattern)
    csrf_processing_done = getattr(django_request, "csrf_processing_done", None)
    if csrf_processing_done is not None:
        bolt_request.state["csrf_processing_done"] = csrf_processing_done

    csrf_cookie_needs_reset = getattr(django_request, "csrf_cookie_needs_reset", None)
    if csrf_cookie_needs_reset is not None:
        bolt_request.state["csrf_cookie_needs_reset"] = csrf_cookie_needs_reset

    # Copy custom middleware attributes, such as the ``tenant`` of
    # django-tenants. An async handler can then read them on any thread.
    for name, value in django_request.__dict__.items():
        if name not in _STANDARD_REQUEST_ATTRS and name[0] != "_":
            bolt_request.state[name] = value


def _extract_preserved_body(response: Any) -> tuple[int, Any] | None:
    """Return special file/stream body payloads that must survive middleware hops."""
    body_kind = getattr(response, "_body_kind", None)
    if body_kind not in _PRESERVED_BODY_KINDS or not hasattr(response, "body"):
        return None
    return body_kind, response.body


def _store_preserved_body(django_response: HttpResponse, preserved_body: tuple[int, Any] | None) -> None:
    """Attach preserved transport metadata to a Django response when needed."""
    if preserved_body is None:
        return

    body_kind, body = preserved_body
    setattr(django_response, _PRESERVED_BODY_ATTR, body)
    setattr(django_response, _PRESERVED_BODY_KIND_ATTR, body_kind)


def _load_preserved_body(django_response: HttpResponse) -> tuple[int, Any] | None:
    """Load preserved transport metadata from a Django response if present."""
    body_kind = getattr(django_response, _PRESERVED_BODY_KIND_ATTR, None)
    if body_kind not in _PRESERVED_BODY_KINDS:
        return None

    body = getattr(django_response, _PRESERVED_BODY_ATTR, None)
    if body is None:
        return None

    return body_kind, body


def _to_django_response(response: Response) -> HttpResponse:
    """Convert Bolt Response/MiddlewareResponse to Django HttpResponse.

    Also handles Django HttpResponse pass-through (from decorators like @login_required).
    """
    # Fast path: if already a Django HttpResponse, return as-is
    if isinstance(response, HttpResponse):
        return response

    # Handle different response types
    preserved_body = _extract_preserved_body(response)

    if hasattr(response, "body"):
        # MiddlewareResponse has .body
        if preserved_body is not None:
            # Preserve file/stream payloads across Django middleware instead of
            # coercing them into plain response bytes (which turns file bodies
            # into raw filesystem paths and drops streaming semantics).
            content = b""
        else:
            content = response.body if isinstance(response.body, bytes) else str(response.body).encode()
    elif hasattr(response, "to_bytes"):
        content = response.to_bytes()
    elif hasattr(response, "content"):
        content = response.content if isinstance(response.content, bytes) else str(response.content).encode()
    else:
        content = b""

    status_code = getattr(response, "status_code", 200)
    headers = getattr(response, "headers", {})
    content_type, remaining_headers = _split_content_type_headers(headers)

    django_response = HttpResponse(
        content=content,
        status=status_code,
        content_type=content_type or _DEFAULT_DJANGO_RESPONSE_CONTENT_TYPE,
    )

    if remaining_headers:
        for key, value in remaining_headers:
            django_response[key] = value

    # Transfer raw cookies onto the Django HttpResponse so they survive any
    # downstream Django middleware (e.g. LocaleMiddleware) that returns the
    # same response object. Without this, _to_bolt_response() harvests an
    # empty cookie jar and handler-set cookies are silently dropped.
    #
    # Write directly to the SimpleCookie morsel instead of HttpResponse.set_cookie:
    # raw tuples are already validated by Cookie.to_raw_tuple(), so we skip
    # http_date(time.time()+max_age), int(max_age), samesite validation, and
    # the unconditional empty-expires write that set_cookie performs.
    raw_cookies = getattr(response, "_raw_cookies", None)
    if raw_cookies:
        cookies = django_response.cookies
        for name, value, path, max_age, expires, domain, secure, httponly, samesite in raw_cookies:
            cookies[name] = value
            morsel = cookies[name]
            if max_age is not None:
                morsel["max-age"] = max_age
            if expires:
                morsel["expires"] = expires
            if path:
                morsel["path"] = path
            if domain:
                morsel["domain"] = domain
            if secure:
                morsel["secure"] = True
            if httponly:
                morsel["httponly"] = True
            if samesite:
                morsel["samesite"] = samesite

    _store_preserved_body(django_response, preserved_body)
    return django_response


def _to_bolt_response(django_response: HttpResponse) -> MiddlewareResponse:
    """Convert Django HttpResponse to MiddlewareResponse for chain compatibility."""
    headers = dict(django_response.items())
    preserved_body = _load_preserved_body(django_response)

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

    body = django_response.content
    response_type = "json"
    body_kind = _BODY_BYTES

    if preserved_body is not None:
        body_kind, body = preserved_body
        response_type = "file" if body_kind == _BODY_FILE else "streaming"
        # The final Rust file/stream response computes its own transport length.
        headers = {k: v for k, v in headers.items() if k.lower() not in _REBUILT_BODY_HEADER_NAMES}

    return MiddlewareResponse(
        status_code=django_response.status_code,
        headers=headers,
        body=body,
        response_type=response_type,
        raw_cookies=raw_cookies,
        body_kind=body_kind,
    )


__all__ = ["DjangoMiddleware", "DjangoMiddlewareStack"]
