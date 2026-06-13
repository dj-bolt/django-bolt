import contextlib

import msgspec
from users.api import UserMini
from users.models import User

from django_bolt import (
    BoltAPI,
    WebSocket,
)
from django_bolt.middleware import BaseMiddleware, TimingMiddleware
from django_bolt.shortcuts import render
from django_bolt.types import Request

# ============================================================================
# Custom Middleware Example
# ============================================================================


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


# OpenAPI is enabled by default at /docs with Swagger UI
# You can customize it by passing openapi_config:
#
# Example compression configurations:
#
# 1. Default compression (brotli with gzip fallback):
api = BoltAPI()
#
# 2. Custom compression with specific settings:
# api = BoltAPI(
#     compression=CompressionConfig(
#         backend="brotli",           # Primary backend: "brotli", "gzip", or "zstd"
#         minimum_size=500,            # Don't compress responses smaller than this (bytes)
#         gzip_fallback=True,          # Fall back to gzip if client doesn't support primary backend
#     )
# )
#
# 3. Gzip-only configuration:
# api = BoltAPI(
#     compression=CompressionConfig(
#         backend="gzip",
#         minimum_size=1000,
#         gzip_fallback=False,         # No fallback needed for gzip
#     )
# )
#
# 4. Zstd compression with gzip fallback:
# api = BoltAPI(
#     compression=CompressionConfig(
#         backend="zstd",
#         minimum_size=2000,           # Only compress larger responses
#         gzip_fallback=True,
#     )
# )

# Using default compression configuration


class Item(msgspec.Struct):
    name: str
    price: float
    is_offer: bool | None = None


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


@middleware_api.get("/demo")
async def middleware_demo(request: Request):
    from django.contrib import messages  # noqa: PLC0415

    # Add messages using Django's messages framework
    messages.error(request, "This is an error message")
    # Access Django user
    user = await request.auser()
    # Render template that displays messages
    return render(
        request,
        "messages_demo.html",
        {
            "title": "Middleware & Messages Demo",
            "user": user,
            "request_id": request.state.get("request_id"),
            "tenant_id": request.state.get("tenant_id"),
        },
    )


# Mount the middleware API as a sub-application (FastAPI-style)
api.mount("/middleware", middleware_api)


@api.websocket("/ws/room/{room_id}")
async def websocket_room(websocket: WebSocket, room_id: str):
    await websocket.accept()
    # Client disconnected
    with contextlib.suppress(Exception):
        async for message in websocket.iter_text():
            await websocket.send_text(f"[{room_id}] {message}")


@api.get("/users")
async def read_users_async() -> list[UserMini]:
    users = User.objects.all()[0:100]
    return users
