from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterable
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Protocol

import msgspec
import test_data
from django.contrib.auth import aauthenticate, get_user_model

from django_bolt import (
    BoltAPI,
    WebSocket,
)
from django_bolt.auth import IsAuthenticated, JWTAuthentication, create_jwt_for_user, get_current_user
from django_bolt.exceptions import (
    HTTPException,
    NotFound,
    RequestValidationError,
    Unauthorized,
    UnprocessableEntity,
)
from django_bolt.health import add_health_check
from django_bolt.middleware import no_compress
from django_bolt.openapi import OpenAPIConfig
from django_bolt.param_functions import Cookie, Depends, File, Form, Header
from django_bolt.responses import (
    HTML,
    JSON,
    EventSourceResponse,
    FileResponse,
    PlainText,
    Redirect,
    StreamingResponse,
)
from django_bolt.types import Request
from django_bolt.views import APIView

from .middleware_demo import middleware_api


@asynccontextmanager
async def lifespan(app):
    """Run startup/shutdown logic for the application."""
    print("Starting up — warming caches, initializing resources...")
    app._startup_time = time.time()
    yield
    uptime = time.time() - app._startup_time
    print(f"Shutting down — server was up for {uptime:.1f}s")


# OpenAPI is enabled by default at /docs with Swagger UI
# You can customize it by passing openapi_config:
#
# Example compression configurations:
#
# 1. Default compression (brotli with gzip fallback):
api = BoltAPI(
    lifespan=lifespan,
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        enabled=True,
    ),
)


@api.get("/", tags=["root"], summary="summary", description="description")
async def read_root():
    """
    Endpoint that returns a simple "Hello World" dictionary.
    """
    return {"message": "Hello Django"}


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


class PostActivity(msgspec.Struct, tag="post"):
    id: int
    actor: str
    title: str
    body: str
    created_at: str


class CommentActivity(msgspec.Struct, tag="comment"):
    id: int
    actor: str
    post_id: int
    text: str
    created_at: str


class LikeActivity(msgspec.Struct, tag="like"):
    id: int
    actor: str
    target_id: int
    target_kind: str
    created_at: str


FeedItem = PostActivity | CommentActivity | LikeActivity


@api.get("/feed/{item_id}", response_model=FeedItem)
async def feed_item(item_id: int) -> FeedItem:
    kind = item_id % 3
    if kind == 0:
        return PostActivity(
            id=item_id,
            actor="alice",
            title="Shipping a new feature",
            body="Today we rolled out polymorphic feed responses.",
            created_at="2026-05-24T10:00:00Z",
        )
    if kind == 1:
        return CommentActivity(
            id=item_id,
            actor="bob",
            post_id=item_id - 1,
            text="Nice work — the tagged union serialization is fast.",
            created_at="2026-05-24T10:05:00Z",
        )
    return LikeActivity(
        id=item_id,
        actor="carol",
        target_id=item_id - 2,
        target_kind="post",
        created_at="2026-05-24T10:10:00Z",
    )


@api.get("/feed", response_model=list[FeedItem])
async def feed() -> list[FeedItem]:
    out: list[FeedItem] = []
    for i in range(100):
        kind = i % 3
        if kind == 0:
            out.append(
                PostActivity(
                    id=i,
                    actor=f"user{i}",
                    title=f"Post {i}",
                    body="Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
                    created_at="2026-05-24T10:00:00Z",
                )
            )
        elif kind == 1:
            out.append(
                CommentActivity(
                    id=i,
                    actor=f"user{i}",
                    post_id=i - 1,
                    text="Great post, thanks for sharing!",
                    created_at="2026-05-24T10:05:00Z",
                )
            )
        else:
            out.append(
                LikeActivity(
                    id=i,
                    actor=f"user{i}",
                    target_id=i - 2,
                    target_kind="post",
                    created_at="2026-05-24T10:10:00Z",
                )
            )
    return out


# ============================================================================
# Middleware Demo - Separate API with Django + Custom Middleware
# ============================================================================

# Mount the middleware API as a sub-application (FastAPI-style)
# This preserves the middleware_api's own middleware configuration
api.mount("/middleware", middleware_api)


# ============================================================================
# ASGI Mount Demo
# ============================================================================


async def asgi_mount_demo(scope, receive, send):
    """
    Simple ASGI app mounted under /asgi-demo.

    Try:
    - /asgi-demo
    - /asgi-demo/hello?name=bolt
    """
    payload = {
        "source": "mount_asgi",
        "type": scope.get("type"),
        "method": scope.get("method"),
        "root_path": scope.get("root_path"),
        "path": scope.get("path"),
        "query_string": scope.get("query_string", b"").decode("latin-1"),
    }
    body = json.dumps(payload).encode("utf-8")

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


# Generic HTTP ASGI mount demo (scope/root_path/path behavior)
api.mount_asgi("/asgi-demo", asgi_mount_demo)

# Mount Django URLconf as a sub-application:
# - /django/admin/
# - /django/
# - /django/accounts/
# - /django/accounts/login/
# - /django/accounts/profile/
# - /django/accounts/provider/callback/?code=demo&state=xyz
# - /django/accounts/allauth/ (if django-allauth is installed/configured)
api.mount_django("/django")


@api.get("/health")
async def health():
    """Health check endpoint (TenantMiddleware skips this path)."""
    return {"status": "healthy", "timestamp": time.time()}


@api.post("/items")
async def create_item(item: Item):
    """Create a new item."""
    return {"item": item, "created": True}


class CustomRequest(Request, Protocol):
    """Extended Request type with custom properties"""

    # Inherited from Request:
    # - method, path, body, context, user
    # - get(), __getitem__()

    # If you add custom request properties via middleware:
    tenant_id: str | None
    request_id: str


# ==== Authentication Examples - JWT Auth with request.user ====
@api.get(
    "/auth/me",
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated()],
    tags=["auth"],
    summary="Get current authenticated user",
)
async def get_me(request: CustomRequest):
    """
    Returns the authenticated user's information using request.user.

    Requires a valid JWT token in the Authorization header:
    Authorization: Bearer <jwt_token>

    The request.user property is automatically populated by the authentication
    system and contains the Django User instance for the authenticated user.
    """
    user = request.user
    if not user:
        return {"error": "User not authenticated"}

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "is_active": user.is_active,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


@api.get(
    "/auth/me-dependency",
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated()],
    tags=["auth"],
    summary="Get current user via dependency injection",
)
async def get_me_dependency(user=Depends(get_current_user)):
    """
    Alternative endpoint that uses dependency injection to get the current user.

    This demonstrates the `get_current_user` dependency which is useful when
    you want to ensure the user is loaded early and available for other operations.

    Requires a valid JWT token in the Authorization header:
    Authorization: Bearer <jwt_token>
    """
    if not user:
        return {"error": "User not authenticated"}

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "is_active": user.is_active,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


@api.get(
    "/auth/context",
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated()],
    tags=["auth"],
    summary="Get authentication context",
)
async def get_auth_context(request: Request):
    """
    Returns the raw authentication context (header-based access).

    This shows what's available in the request.context dictionary,
    including auth backend info and user claims from the JWT.

    Requires a valid JWT token in the Authorization header:
    Authorization: Bearer <jwt_token>
    """
    context = request.context

    return {
        "user_id": context.get("user_id"),
        "is_staff": context.get("is_staff"),
        "is_superuser": context.get("is_superuser"),
        "auth_backend": context.get("auth_backend"),
        "permissions": context.get("permissions", []),
        "auth_claims": context.get("auth_claims", {}),
    }


class TokenRequest(msgspec.Struct):
    """Request body for token generation."""

    username: str
    password: str


@api.post("/auth/token", tags=["auth"], summary="Generate access token")
async def generate_token(token_req: TokenRequest):
    """
    Generate a JWT access token for a user.

    Accepts username and password, validates them, and returns a JWT token.

    Request body:
    ```json
    {
        "username": "john",
        "password": "password123"
    }
    ```

    Response on success:
    ```json
    {
        "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
        "token_type": "bearer",
        "expires_in": 3600,
        "user": {
            "id": 1,
            "username": "john",
            "email": "john@example.com",
            "is_staff": false,
            "is_superuser": false
        }
    }
    ```

    Response on failure (invalid credentials):
    ```json
    {
        "error": "Invalid username or password"
    }
    ```
    """
    get_user_model()

    # Authenticate the user
    user = await aauthenticate(username=token_req.username, password=token_req.password)

    if user is None:
        raise Unauthorized(detail="Invalid username or password")

    # Generate JWT token
    access_token = create_jwt_for_user(user, expires_in=3600)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
        },
    }


@api.get("/sync", tags=["root"], summary="summary", description="description")
def read_root_sync():
    """
    Endpoint that returns a simple "Hello World" dictionary.
    """
    return {"message": "Hello World"}


@api.get("/10k-json")
async def read_10k():
    """
    Endpoint that returns 10k JSON objects.

    """
    return test_data.JSON_10K


class ItemSchema(msgspec.Struct):
    id: int
    name: str
    description: str
    price: float
    category: str
    in_stock: bool
    tags: list[str]


@api.get("/1k-json", validate_response=False)
async def read_1k() -> list[ItemSchema]:
    """
    Endpoint that returns 10k JSON objects.

    """
    return test_data.JSON_1K


@api.get("/sync-10k-json")
def read_10k_sync():
    """
    Sync version: Endpoint that returns 10k JSON objects.

    """
    return test_data.JSON_10K


@api.get("/items/{item_id}")
async def read_item(item_id: int, q: str | None = None):
    return {"item_id": item_id, "q": q}


@api.put("/items/{item_id}", response_model=dict)
async def update_item(item_id: int, item: Item) -> dict:
    return {"item_name": item.name, "item_id": item_id}


# ==== Benchmarks: JSON parsing/validation & slow async op ====
class BenchPayload(msgspec.Struct):
    title: str
    count: int
    items: list[Item]


@api.post("/bench/parse")
async def bench_parse(req: Request, payload: BenchPayload):
    # msgspec validates and decodes in one pass; just return minimal data

    return {"ok": True, "n": len(payload.items), "count": payload.count}


# ==== Benchmark endpoints for Header/Cookie/Exception/HTML/Redirect ====
@api.get("/header")
async def get_header(x: Annotated[str, Header(alias="x-test")]):
    return PlainText(x)


@api.get("/cookie")
async def get_cookie(val: Annotated[str, Cookie(alias="session")]):
    return PlainText(val)


# ==== Cookie Setting Demo Endpoints ====
# These demonstrate the cookie API and Rust-side validation


@api.get("/cookies/valid")
async def cookies_valid():
    """
    Set a valid cookie with all standard attributes.

    Test with:
        curl -v http://localhost:8000/cookies/valid

    Response headers should include:
        Set-Cookie: session=abc123; Path=/; Max-Age=3600; Secure; HttpOnly; SameSite=Lax
    """
    response = JSON({"message": "Cookie set successfully", "cookie_name": "session"})
    response.set_cookie(
        "session",
        "abc123",
        max_age=3600,
        path="/",
        secure=False,
        httponly=True,
        samesite="Lax",
    )
    return response


@api.get("/cookies/delete")
async def cookies_delete():
    """
    Delete a cookie by setting it to expire immediately.

    Test with:
        curl -v http://localhost:8000/cookies/delete

    Response headers should include:
        Set-Cookie: session=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT
    """
    response = JSON({"message": "Cookie deleted", "cookie_name": "session"})
    response.delete_cookie("session")
    return response


@api.get("/exc")
async def raise_exc():
    raise HTTPException(status_code=404, detail="Not found")


@api.get("/html")
async def get_html():
    return HTML("<h1>Hello</h1>")


@api.get("/redirect")
async def get_redirect():
    return Redirect("/", status_code=302)


# ==== Form and File upload endpoints ====
@api.post("/form")
async def handle_form(
    name: Annotated[str, Form()], age: Annotated[int, Form()], email: Annotated[str, Form()] = "default@example.com"
):
    return {"name": name, "age": age, "email": email}


@api.post("/upload")
async def handle_upload(files: Annotated[list[dict], File(alias="file")]):
    # Return file metadata
    return {"uploaded": len(files), "files": [{"name": f.get("filename"), "size": f.get("size")} for f in files]}


class FormRepeatedKeys(msgspec.Struct):
    name: str
    tags: list[str] = []
    counts: list[int] = []


@api.post("/form-list")
async def handle_form_list(data: Annotated[FormRepeatedKeys, Form()]):
    """
    Handle a submitted form containing repeated keys and return summary statistics.

    Parameters:
        data (FormRepeatedKeys): Parsed form payload with fields:
            - name: submitted name string
            - tags: list of submitted tag strings (may be empty)
            - counts: list of submitted integers corresponding to counts (may be empty)

    Returns:
        dict: Summary with keys:
            - name (str): the submitted name
            - tag_count (int): number of submitted tags
            - count_sum (int): sum of submitted counts
    """
    return {"name": data.name, "tag_count": len(data.tags), "count_sum": sum(data.counts)}


# ==== File serving endpoint for benchmarks ====
THIS_FILE = os.path.abspath(__file__)


@api.get("/file-static")
async def file_static():
    return FileResponse(THIS_FILE, filename="api.py")


# ==== Streaming endpoints for benchmarks ====
@api.get("/stream")
@no_compress
async def stream_plain():
    def gen():
        for _i in range(100):
            yield "x"

    return StreamingResponse(gen(), media_type="text/plain")


@api.get("/sse")
async def sse():
    async def gen():
        while True:
            await asyncio.sleep(1)
            yield f"data: {time.time()}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ==== EventSourceResponse (new SSE syntax) ====


class TimestampEvent(msgspec.Struct):
    timestamp: float
    count: int


@api.get("/sse-new", response_class=EventSourceResponse)
async def sse_new() -> AsyncIterable[TimestampEvent]:
    """Implicit SSE: generator handler + response_class. No @no_compress needed."""
    count = 0
    while True:
        await asyncio.sleep(1)
        count += 1
        yield TimestampEvent(timestamp=time.time(), count=count)


# ==== Error Handling & Logging Examples ====


# Example 1: Using specialized HTTP exceptions
@api.get("/errors/not-found/{resource_id}")
async def error_not_found(resource_id: int):
    """Example of NotFound exception with custom message."""
    if resource_id == 0:
        raise NotFound(detail=f"Resource {resource_id} not found")
    return {"resource_id": resource_id, "status": "found"}


class UserCreate(msgspec.Struct):
    username: str
    email: str
    age: int


@api.post("/errors/validation")
async def error_validation(user: UserCreate):
    """Example of manual validation with RequestValidationError."""
    errors = []

    if len(user.username) < 3:
        errors.append(
            {
                "loc": ["body", "username"],
                "msg": "Username must be at least 3 characters",
                "type": "value_error.min_length",
            }
        )

    if "@" not in user.email:
        errors.append(
            {
                "loc": ["body", "email"],
                "msg": "Invalid email format",
                "type": "value_error.email",
            }
        )

    if user.age < 0 or user.age > 150:
        errors.append(
            {
                "loc": ["body", "age"],
                "msg": "Age must be between 0 and 150",
                "type": "value_error.range",
            }
        )

    if errors:
        raise RequestValidationError(errors, body=user)

    return {"status": "created", "user": user}


# Example 3: Generic exception (will show traceback in DEBUG mode)
@api.get("/errors/complex")
async def error_complex():
    """Example of HTTPException with extra structured data."""
    raise UnprocessableEntity(
        detail="Multiple validation errors occurred",
        extra={
            "errors": [
                {"field": "email", "reason": "Email already exists"},
                {"field": "username", "reason": "Username contains invalid characters"},
            ],
            "suggestion": "Please correct the highlighted fields",
            "documentation": "https://api.example.com/docs/validation",
        },
    )


# Example 5: Custom health check
async def check_external_api():
    """Custom health check for external API."""
    try:
        # Simulate checking external service
        # In real app: await httpx.get("https://api.example.com/health")
        await asyncio.sleep(0.001)
        return True, "External API OK"
    except Exception as e:
        return False, f"External API error: {str(e)}"


# Add custom health check to /ready endpoint
add_health_check(check_external_api)


# ============================================================================
# Multi-Response Example
#
# response_model accepts a dict mapping status codes to schemas, enabling
# per-status validation and accurate OpenAPI documentation.
#
# Return conventions:
#   (status_code, data)  — validates data against the schema for that code
#   bare dict/list       — validated against the default (lowest 2xx) schema
#   {204: None}          — return (204, None) for an empty-body response
#   {...: ErrorSchema}   — ellipsis is a catch-all for any unmapped code
# ============================================================================


class ItemOut(msgspec.Struct):
    """A catalog item."""

    id: int
    name: str
    price: float


class ItemIn(msgspec.Struct):
    """Payload for creating an item."""

    name: str
    price: float


class ValidationError(msgspec.Struct):
    """Validation failure details."""

    detail: str
    field: str | None = None


# Tiny in-memory store so the example is self-contained.
_ITEMS: dict[int, dict] = {
    1: {"id": 1, "name": "Widget", "price": 9.99},
    2: {"id": 2, "name": "Gadget", "price": 24.99},
}
_NEXT_ID = 3


@api.get(
    "/multi/items/{item_id}",
    response_model={200: ItemOut, 404: ValidationError},
    tags=["Multi-Response"],
    summary="Get item by ID",
)
async def multi_get_item(item_id: int):
    """
    Returns 200 + ItemOut when the item exists, 404 + ValidationError otherwise.

    Try:
        curl http://localhost:8000/multi/items/1
        curl http://localhost:8000/multi/items/99
    """
    item = _ITEMS.get(item_id)
    if item is None:
        return 404, {"detail": f"Item {item_id} not found"}
    return 200, item


@api.post(
    "/multi/items",
    response_model={201: ItemOut, 400: ValidationError},
    status_code=201,
    tags=["Multi-Response"],
    summary="Create an item",
)
async def multi_create_item(payload: ItemIn):
    """
    Returns 201 + ItemOut on success, 400 + ValidationError when price ≤ 0.

    Try:
        curl -X POST http://localhost:8000/multi/items \\
             -H 'Content-Type: application/json' \\
             -d '{"name": "Doohickey", "price": 4.50}'

        curl -X POST http://localhost:8000/multi/items \\
             -H 'Content-Type: application/json' \\
             -d '{"name": "Free?", "price": -1}'
    """
    global _NEXT_ID
    if payload.price <= 0:
        return 400, {"detail": "Price must be greater than zero", "field": "price"}
    item = {"id": _NEXT_ID, "name": payload.name, "price": payload.price}
    _ITEMS[_NEXT_ID] = item
    _NEXT_ID += 1
    return 201, item


@api.delete(
    "/multi/items/{item_id}",
    response_model={204: None, 404: ValidationError},
    tags=["Multi-Response"],
    summary="Delete an item",
)
async def multi_delete_item(item_id: int):
    """
    Returns 204 (empty body) on success, 404 + ValidationError when not found.

    Try:
        curl -X DELETE http://localhost:8000/multi/items/1
        curl -X DELETE http://localhost:8000/multi/items/99
    """
    if item_id not in _ITEMS:
        return 404, {"detail": f"Item {item_id} not found"}
    del _ITEMS[item_id]
    return 204, None


# ============================================================================
# Class-Based Views (APIView) - Using Decorator Syntax
# ============================================================================


@api.view("/cbv-simple", tags=["CBV Benchmark"])
class SimpleAPIView(APIView):
    """Simple APIView for benchmarking."""

    async def get(self, request):
        """GET /cbv-simple - Simple GET endpoint."""
        return {"message": "Hello from APIView"}

    async def post(self, request, data: Item):
        """POST /cbv-simple - POST with validation."""
        return {"name": data.name, "price": data.price, "cbv": True}


@api.view("/cbv-items/{item_id}")
class ItemAPIView(APIView):
    """APIView for item operations."""

    async def get(self, request, item_id: int, q: str | None = None):
        """GET /cbv-items/{item_id} - Get item with optional query param."""
        return {"item_id": item_id, "q": q, "cbv": True}

    async def put(self, request, item_id: int, item: Item):
        """PUT /cbv-items/{item_id} - Update item."""
        return {"item_name": item.name, "item_id": item_id, "cbv": True}


# ============================================================================
# WebSocket Endpoints
# ============================================================================


@api.websocket("/ws")
async def websocket_load_test(websocket: WebSocket):
    """
    WebSocket endpoint for load testing.

    Echoes back any message received. Designed for ws_load.py script.

    Test with:
        python scripts/ws_load.py ws://localhost:8000/ws -c 50 -d 10
    """
    await websocket.accept()
    # Loop until the client disconnects (which raises inside iter_text()).
    with suppress(Exception):
        async for message in websocket.iter_text():
            await websocket.send_text(message)


@api.websocket("/ws/room/{room_id}")
async def websocket_room(websocket: WebSocket, room_id: str):
    """
    WebSocket echo endpoint with room ID path parameter.

    Echoes back messages with the room ID prefix.

    Test with:
        websocat ws://localhost:8000/ws/room/lobby
    """
    await websocket.accept()
    # Loop until the client disconnects (which raises inside iter_text()).
    with suppress(Exception):
        async for message in websocket.iter_text():
            await websocket.send_text(f"[{room_id}] {message}")
