from typing import Annotated

import msgspec
from django.conf import settings
from django.contrib import messages  # noqa: PLC0415
from django.contrib.auth import alogin, alogout
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt

from django_bolt import BoltAPI, Request, Router
from django_bolt.middleware import BaseMiddleware, Middleware, TimingMiddleware, middleware
from django_bolt.params import Form
from django_bolt.shortcuts import render
from django_bolt.views import APIView


class RequestIdMiddleware:
    """
    Custom middleware that adds a request ID to every request.

    Follows Django's middleware pattern:
    - __init__(get_response): Called ONCE at startup
    - __call__(request): Called for each request
    """

    def __init__(self, get_response):
        """Called once at server startup - do expensive setup here."""
        self.get_response = get_response
        self.request_count = 0
        print("[RequestIdMiddleware] Initialized at startup")

    async def __call__(self, request):
        """Called for each request."""
        import uuid  # noqa: PLC0415

        # Generate request ID and add to request state
        request_id = str(uuid.uuid4())[:8]
        self.request_count += 1
        request.state["request_id"] = request_id
        request.state["request_number"] = self.request_count

        # Process the request
        response = await self.get_response(request)

        # Add header to response
        response.headers["X-Request-ID"] = request_id
        return response


class TenantMiddleware(BaseMiddleware):
    """
    Custom middleware with path exclusions using BaseMiddleware helper.

    BaseMiddleware provides:
    - exclude_paths: Glob patterns to skip (compiled once at startup)
    - exclude_methods: HTTP methods to skip (O(1) lookup)
    """

    exclude_paths = ["/health", "/docs", "/docs/*", "/openapi.json"]
    exclude_methods = ["OPTIONS"]

    async def process_request(self, request):
        """Extract tenant from header and add to request state."""
        tenant_id = request.headers.get("x-tenant-id", "default")
        request.state["tenant_id"] = tenant_id
        request.state["tenant_loaded"] = True

        response = await self.get_response(request)

        response.headers["X-Tenant-ID"] = tenant_id
        return response


# Create a separate API instance with middleware enabled
# This demonstrates how to use Django middleware + custom Python middleware
middleware_api = BoltAPI(
    # Load Django middleware from settings.MIDDLEWARE
    django_middleware=True,
    # Add custom Python middleware (pass classes, not instances)
    middleware=[
        RequestIdMiddleware,  # Adds X-Request-ID header
        TenantMiddleware,  # Adds tenant context (skips /health, /docs)
        TimingMiddleware,  # Built-in: adds X-Response-Time header
    ],
)


# =============================================================================
# DOCS EXAMPLES - verify middleware docs snippets are runnable
# =============================================================================


@middleware
async def docs_timing_middleware(request, call_next):
    """Function-style route middleware example from docs."""
    request.state["timing_enabled"] = True
    response = await call_next(request)
    response.headers["X-Timing"] = "enabled"
    return response


class DocsRouteMiddleware(Middleware):
    """Class-based route middleware example from docs."""

    async def process_request(self, request):
        request.state["docs_route_middleware"] = True
        response = await self.get_response(request)
        response.headers["X-Docs-Route"] = "true"
        return response


class DocsRouterMiddleware(Middleware):
    """Class-based router middleware example from docs."""

    async def process_request(self, request):
        request.state["docs_router_middleware"] = True
        response = await self.get_response(request)
        response.headers["X-Docs-Router"] = "true"
        return response


docs_router = Router(
    prefix="/docs/router",
    middleware=[DocsRouterMiddleware],
)


@docs_router.get("/users")
async def docs_router_users(request: Request):
    return {
        "items": [],
        "docs_router_middleware": request.state.get("docs_router_middleware", False),
        "request_id": request.state.get("request_id"),
        "tenant_id": request.state.get("tenant_id"),
    }


middleware_api.include_router(docs_router)


@middleware_api.get("/docs/function")
@docs_timing_middleware
async def docs_function_middleware_example(request: Request):
    """Function-style middleware usage example."""
    return {
        "ok": True,
        "timing_enabled": request.state.get("timing_enabled", False),
        "request_id": request.state.get("request_id"),
        "tenant_id": request.state.get("tenant_id"),
    }


@middleware_api.get("/docs/class-route")
@middleware(DocsRouteMiddleware)
async def docs_class_route_middleware_example(request: Request):
    """Class-based per-route middleware usage example."""
    return {
        "ok": True,
        "docs_route_middleware": request.state.get("docs_route_middleware", False),
        "request_id": request.state.get("request_id"),
        "tenant_id": request.state.get("tenant_id"),
    }


@middleware_api.get("/docs/class-global")
async def docs_class_global_middleware_example(request: Request):
    """Class-based global middleware usage example (from BoltAPI middleware list)."""
    return {
        "ok": True,
        "request_id": request.state.get("request_id"),
        "tenant_id": request.state.get("tenant_id"),
    }


@middleware_api.get("/demo")
async def middleware_demo(request: Request):
    """
    Demonstrates Django middleware + messages framework with Django-Bolt.

    This endpoint shows:
    1. Django middleware (SessionMiddleware, AuthenticationMiddleware, MessageMiddleware)
    2. Custom RequestIdMiddleware (adds X-Request-ID header)
    3. Custom TenantMiddleware (adds X-Tenant-ID header)
    4. Django messages framework ({% for message in messages %} in templates)

    Test with:
        curl http://localhost:8000/middleware/demo
    """

    # Add messages using Django's messages framework
    messages.info(request, "This is an info message")
    # Access Django user
    # user = await request.auser()

    # Render template that displays messages
    return render(
        request,
        "messages_demo.html",
        {
            "title": "Middleware & Messages Demo",
            # "user": user,
            "request_id": request.state.get("request_id"),
            "tenant_id": request.state.get("tenant_id"),
        },
    )


@middleware_api.post("/demo")
# @csrf_exempt
async def middleware_demo_post(request: Request, test: Annotated[str, Form("test")]):
    """
    Demonstrates Django middleware + messages framework with Django-Bolt.

    This endpoint shows:
    1. Django middleware (SessionMiddleware, AuthenticationMiddleware, MessageMiddleware)
    2. Custom RequestIdMiddleware (adds X-Request-ID header)
    3. Custom TenantMiddleware (adds X-Tenant-ID header)
    4. Django messages framework ({% for message in messages %} in templates)

    Test with:
        curl http://localhost:8000/middleware/demo
    """
    print(test)
    # Add messages using Django's messages framework
    messages.info(request, "This is an info message")
    messages.success(request, "Operation completed successfully!")
    messages.warning(request, "This is a warning message")
    messages.error(request, "This is an error message")

    # Access Django user
    # user = await request.auser()

    # Render template that displays messages
    return render(
        request,
        "messages_demo.html",
        {
            "title": "Middleware & Messages Demo",
            # "user": user,
            "request_id": request.state.get("request_id"),
            "tenant_id": request.state.get("tenant_id"),
        },
    )


# =============================================================================
# TEST ENDPOINTS FOR PR #93 - Middleware Safety & AST Analysis Issues
# =============================================================================
# These endpoints demonstrate the issues fixed in PR #93:
# 1. Unsafe middleware (Session, Auth, Message) causing SynchronousOnlyOperation
# 2. AST analyzer missing ORM calls in nested function calls
# 3. APIView.as_view() wrapper hiding ORM detection from AST analyzer


class RequestSerializer(msgspec.Struct):
    """Simple request body for testing."""

    username: str


class UserService:
    """Service class that performs ORM operations - AST may miss these."""

    def sync_get_user(self, request):
        """Sync method that does ORM - called from handler."""
        # AST analyzer may not detect this ORM call when called from handler
        user = User.objects.filter(username="test").first()
        return user

    async def async_get_user(self, request):
        """Async method that does ORM - called from handler."""
        user = await User.objects.filter(username="test").afirst()
        return user


# Singleton service instance
_user_service = UserService()


# -----------------------------------------------------------------------------
# Issue 1: Unsafe middleware causing SynchronousOnlyOperation
# -----------------------------------------------------------------------------
# SessionMiddleware, AuthenticationMiddleware, MessageMiddleware perform
# blocking I/O but were classified as "safe". This causes SynchronousOnlyOperation
# when used with async handlers that do ORM operations.


@middleware_api.post("/test/async-orm")
@csrf_exempt
async def test_async_orm(request: Request, data: RequestSerializer):
    """
    Test async handler with ORM + Django middleware.

    Before fix: SynchronousOnlyOperation because Session/Auth/Message middleware
    ran synchronously in the async context.

    After fix: Middleware wrapped with sync_to_async, ORM works correctly.

    Test with:
        curl -X POST http://localhost:8001/middleware/test/async-orm \
             -H "Content-Type: application/json" \
             -d '{"username": "testuser"}'
    """
    # This ORM call should work after the middleware safety fix
    user = await User.objects.filter(username=data.username).afirst()

    if user:
        # This also requires middleware to be properly async-wrapped
        await alogin(request, user)
        logged_in_user = await request.auser()
        return {"status": "ok", "user_id": user.id, "logged_in": True, "logged_in_user": logged_in_user.id}

    return {"status": "ok", "user": None, "logged_in": False}


# -----------------------------------------------------------------------------
# Issue 2: AST analyzer missing ORM in nested function calls
# -----------------------------------------------------------------------------
# The AST analyzer inspects handler source but doesn't follow calls into
# other functions/methods. ORM operations in service classes are missed.


@middleware_api.post("/test/sync-nested-orm")
@csrf_exempt
def test_sync_nested_orm(request: Request, data: RequestSerializer):
    """
    Test sync handler calling service with ORM.

    The AST analyzer may not detect the ORM call inside UserService.sync_get_user()
    because it doesn't recursively analyze called functions.

    Test with:
        curl -X POST http://localhost:8000/middleware/test/sync-nested-orm \
             -H "Content-Type: application/json" \
             -d '{"username": "testuser"}'
    """
    # ORM call is hidden inside service method - AST may miss this
    user = _user_service.sync_get_user(request)

    if user:
        return {"status": "ok", "user_id": user.id, "source": "service"}

    return {"status": "ok", "user": None}


@middleware_api.post("/test/async-nested-orm")
@csrf_exempt
async def test_async_nested_orm(request: Request, data: RequestSerializer):
    """
    Test async handler calling async service with ORM.

    Test with:
        curl -X POST http://localhost:8000/middleware/test/async-nested-orm \
             -H "Content-Type: application/json" \
             -d '{"username": "testuser"}'
    """
    # ORM call is hidden inside async service method
    user = await _user_service.async_get_user(request)

    if user:
        return {"status": "ok", "user_id": user.id, "source": "async_service"}

    return {"status": "ok", "user": None}


# -----------------------------------------------------------------------------
# Issue 3: APIView.as_view() wrapper hiding ORM from AST analyzer
# -----------------------------------------------------------------------------
# APIView.as_view() creates a wrapper function. The AST analyzer inspects
# the wrapper (view_handler) instead of the actual method (post/get/etc).
# This causes uses_orm to always be False for CBVs.


@middleware_api.view("/test/cbv-sync-orm")
class CBVSyncORMView(APIView):
    """
    Class-based view with sync ORM operations.

    Before fix: AST analyzer sees view_handler wrapper, misses ORM in post().
    HandlerAnalysis(uses_orm=False, ...)

    After fix: inspect.unwrap() reveals the actual method, ORM detected.
    HandlerAnalysis(uses_orm=True, orm_operations={'all', 'comprehension_all'}, ...)

    Test with:
        curl -X POST http://localhost:8000/middleware/test/cbv-sync-orm \
             -H "Content-Type: application/json" \
             -d '{"username": "testuser"}'
    """

    def post(self, request: Request, data: RequestSerializer):
        """Sync POST with ORM - should be detected by AST."""
        # Direct ORM call in CBV method
        users = User.objects.all()
        # List comprehension over queryset
        result = [user.id for user in users]
        return {"status": "ok", "user_ids": result[:5]}  # Limit for response


# -----------------------------------------------------------------------------
# Session Demo - HTML-based session testing
# -----------------------------------------------------------------------------
# These endpoints test Django's built-in session data saving/reading
# using HTML templates (browser-based testing with cookies)


@middleware_api.get("/session")
async def session_demo_get(request: Request):
    """
    Display session data.

    Test in browser: http://localhost:8001/middleware/session
    """
    user = await request.auser()
    user_info = f"{user.username} (id={user.id})" if user.is_authenticated else "Anonymous"

    # Read session data using Django's native async session methods
    session = request.session
    session_key = session.session_key
    my_key = await session.aget("my_key")
    counter = await session.aget("counter", 0)
    custom_value = await session.aget("custom_value")

    return render(
        request,
        "session_demo.html",
        {
            "session_key": session_key,
            "my_key": my_key,
            "counter": counter,
            "custom_value": custom_value,
            "user_info": user_info,
            "is_authenticated": user.is_authenticated,
        },
    )


@middleware_api.post("/session")
async def session_demo_post(
    request: Request,
    action: str = "",
    username: Annotated[str, Form("username")] = "",
    key: Annotated[str, Form("key")] = "",
    value: Annotated[str, Form("value")] = "",
):
    """
    Save data to session, login, or logout.

    Test in browser: http://localhost:8001/middleware/session
    """
    message = None

    if action == "login":
        if username:
            user = await User.objects.filter(username=username).afirst()
            if user:
                await alogin(request, user)
                message = f"Logged in as {user.username}"
            else:
                message = f"User '{username}' not found"
        else:
            message = "No username provided"

    elif action == "logout":
        await alogout(request)
        message = "Logged out"

    elif action == "increment":
        session = request.session
        counter = await session.aget("counter", 0)
        await session.aset("counter", counter + 1)
        message = f"Counter incremented to {counter + 1}"

    else:
        # Set custom session value
        session_key_name = key or "custom_value"

        if session_key_name and value:
            await request.session.aset(session_key_name, value)
            message = f"Saved '{session_key_name}' = '{value}' to session"
        else:
            message = "No value provided"

    user = await request.auser()
    user_info = f"{user.username} (id={user.id})" if user.is_authenticated else "Anonymous"

    # Read session data using Django's native async session methods
    session = request.session
    session_key = session.session_key
    my_key = await session.aget("my_key")
    counter = await session.aget("counter", 0)
    custom_value = await session.aget("custom_value")

    return render(
        request,
        "session_demo.html",
        {
            "session_key": session_key,
            "my_key": my_key,
            "counter": counter,
            "custom_value": custom_value,
            "user_info": user_info,
            "is_authenticated": user.is_authenticated,
            "message": message,
        },
    )


@middleware_api.view("/test/cbv-async-orm")
class CBVAsyncORMView(APIView):
    """
    Class-based view with async ORM operations.

    Test with:
        curl -X POST http://localhost:8000/middleware/test/cbv-async-orm \
             -H "Content-Type: application/json" \
             -d '{"username": "testuser"}'
    """

    async def post(self, request: Request, data: RequestSerializer):
        """Async POST with ORM - should be detected by AST."""
        user = await User.objects.filter(username=data.username).afirst()

        if user:
            return {"status": "ok", "user_id": user.id}

        return {"status": "ok", "user": None}


# -----------------------------------------------------------------------------
# Static Files Demo - Demonstrates Django {% static %} tag with Actix serving
# -----------------------------------------------------------------------------


@middleware_api.get("/static-demo")
async def static_files_demo(request: Request):
    """
    Demonstrates static file serving with Django templates.

    This endpoint renders a template that uses Django's {% static %} tag.
    The actual static files are served directly by Actix (Rust) for
    maximum performance, while Django handles URL generation.

    Features demonstrated:
    - Django's {% static %} template tag generates correct URLs
    - Actix serves files with proper caching headers (ETag, Last-Modified)
    - Admin static files (CSS, JS, images) served without Python overhead

    Test in browser: http://localhost:8000/middleware/static-demo
    """
    return render(
        request,
        "static_demo.html",
        {
            "title": "Static Files Demo",
            "static_url": getattr(settings, "STATIC_URL", "/static/"),
            "static_root": getattr(settings, "STATIC_ROOT", None),
        },
    )
