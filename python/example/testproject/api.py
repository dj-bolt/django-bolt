import asyncio
import json
import os
import time
from collections.abc import AsyncIterable
from contextlib import asynccontextmanager
from typing import Annotated, Protocol

import msgspec
import test_data
from django.contrib.auth import aauthenticate, get_user_model
from msgspec import Meta
from users.api import UserMini
from users.models import User

from django_bolt import (
    BoltAPI,
    WebSocket,
)
from django_bolt.auth import IsAuthenticated, JWTAuthentication, create_jwt_for_user, get_current_user
from django_bolt.exceptions import (
    BadRequest,
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
    EventSourceResponse,
    FileResponse,
    PlainText,
    Redirect,
    ServerSentEvent,
    StreamingResponse,
)
from django_bolt.serializers import Serializer, field_validator
from django_bolt.types import Request
from django_bolt.views import APIView, ViewSet


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
    return {"message": "Hello World"}


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


@api.get("/items100", response_model=list[Item])
async def items100() -> list[Item]:
    return [Item(name=f"item{i}", price=float(i), is_offer=(i % 2 == 0)) for i in range(100)]


# ============================================================================
# Middleware Demo - Separate API with Django + Custom Middleware
# ============================================================================

# Mount the middleware API as a sub-application (FastAPI-style)
# This preserves the middleware_api's own middleware configuration
from .middleware_demo import middleware_api

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


@api.get(
    "/auth/no-user-access",
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated()],
    tags=["auth"],
    summary="Authenticated endpoint that does NOT access request.user (lazy loading benchmark)",
)
async def get_no_user_access(request: Request):
    """
    Authenticated endpoint that does NOT access request.user.

    This demonstrates lazy loading - the user is never loaded from the database
    because request.user is never accessed. Compare performance with /auth/me
    which does access request.user and triggers a database query.

    Use this endpoint to benchmark the overhead of authentication WITHOUT
    user loading, vs /auth/me which includes user loading.

    Requires a valid JWT token in the Authorization header:
    Authorization: Bearer <jwt_token>
    """
    # Only access auth context - never touch request.user
    context = request.context
    return {
        "authenticated": True,
        "user_id": context.get("user_id"),
        "message": "User was NOT loaded from database (lazy loading)",
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


@api.get("/100k-json")
async def read_100k():
    """
    Endpoint that returns 10k JSON objects.

    """
    return test_data.JSON_100K


@api.get("/500k-json")
async def read_100k():
    """
    Endpoint that returns 10k JSON objects.

    """
    return test_data.JSON_500K


@api.get("/1m-json")
async def read_100k():
    """
    Endpoint that returns 10k JSON objects.

    """
    return test_data.JSON_1M


@api.get("/sync-10k-json")
def read_10k_sync():
    """
    Sync version: Endpoint that returns 10k JSON objects.

    """
    return test_data.JSON_10K


class UserMiniSerializer(Serializer):
    id: int
    username: str


@api.get("/sync-users", response_model=list[UserMiniSerializer])
def read_users_sync():
    """
    Sync version: Endpoint that returns 10k JSON objects.

    """
    users = User.objects.all()[0:100]

    return users


# @api.get("/sync-users")
# def read_10k_sync() -> list[UserMini]:
#     """
#     Sync version: Endpoint that returns 10k JSON objects.

#     """
#     users = User.objects.all()[0:100]

#     return users


@api.get("/async-users")
async def read_users_async() -> list[UserMini]:
    """
    Async version: Endpoint that returns 10k JSON objects.

    """
    users = User.objects.all()[0:100]

    return users


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


@api.get("/bench/slow")
async def bench_slow(ms: int | None = 100):
    # Simulate slow I/O (network) with asyncio.sleep
    delay = max(0, (ms or 0)) / 1000.0
    await asyncio.sleep(delay)
    return {"ok": True, "ms": ms}


# ==== Parameter Handling Benchmark Endpoints ====
@api.get("/bench/params/typed/{id}")
async def bench_typed_params(
    id: int,
    count: int,
    price: float,
    active: bool = True,
):
    return {"id": id, "count": count, "price": price, "active": active}


@api.get("/bench/params/multi-query")
async def bench_multi_query(
    page: int = 1,
    limit: int = 10,
    sort: str = "id",
    order: str = "asc",
    filter_active: bool = True,
    min_price: float = 0.0,
    max_price: float = 1000.0,
):
    return {
        "page": page,
        "limit": limit,
        "sort": sort,
        "order": order,
        "filter_active": filter_active,
        "min_price": min_price,
        "max_price": max_price,
    }


@api.post("/bench/form/typed")
async def bench_typed_form(
    name: Annotated[str, Form()],
    age: Annotated[int, Form()],
    score: Annotated[float, Form()],
    active: Annotated[bool, Form()] = True,
):
    return {"name": name, "age": age, "score": score, "active": active}


@api.post("/bench/form/large")
async def bench_large_form(
    field1: Annotated[str, Form()],
    field2: Annotated[str, Form()],
    field3: Annotated[str, Form()],
    field4: Annotated[str, Form()],
    field5: Annotated[str, Form()],
    num1: Annotated[int, Form()],
    num2: Annotated[int, Form()],
    num3: Annotated[float, Form()],
    flag1: Annotated[bool, Form()] = True,
    flag2: Annotated[bool, Form()] = False,
):
    return {"fields": 10, "ok": True}


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
    from django_bolt.responses import JSON

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


@api.get("/cookies/multiple")
async def cookies_multiple():
    """
    Set multiple cookies in one response.

    Test with:
        curl -v http://localhost:8000/cookies/multiple

    Response headers should include multiple Set-Cookie headers.
    """
    from django_bolt.responses import JSON

    response = JSON({"message": "Multiple cookies set", "cookies": ["user_id", "theme", "lang"]})
    response.set_cookie("user_id", "12345", httponly=True)
    response.set_cookie("theme", "dark", max_age=86400 * 365)  # 1 year
    response.set_cookie("lang", "en-US")
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
    from django_bolt.responses import JSON

    response = JSON({"message": "Cookie deleted", "cookie_name": "session"})
    response.delete_cookie("session")
    return response


@api.get("/cookies/invalid-name")
async def cookies_invalid_name():
    """
    Attempt to set a cookie with an invalid name (contains injection characters).

    The Rust layer will reject this cookie and log a warning.
    The response will succeed but the invalid cookie won't be set.

    Test with:
        curl -v http://localhost:8000/cookies/invalid-name

    Check server logs for:
        [django-bolt] WARNING: Invalid cookie name 'session; Path=/evil' - contains invalid characters

    The Set-Cookie header will NOT appear in the response.
    """
    from django_bolt.responses import JSON

    response = JSON(
        {
            "message": "Attempted to set cookie with invalid name",
            "cookie_name": "session; Path=/evil",
            "note": "This cookie will be rejected by Rust validation - check server logs",
        }
    )
    # This name contains '; Path=/evil' which is a header injection attempt
    # Rust will reject it based on RFC 6265 cookie name validation
    response.set_cookie("session; Path=/evil", "malicious", httponly=True)
    return response


@api.get("/cookies/invalid-value")
async def cookies_invalid_value():
    """
    Attempt to set a cookie with control characters in the value.

    The Rust layer will reject this cookie and log a warning.
    The response will succeed but the invalid cookie won't be set.

    Test with:
        curl -v http://localhost:8000/cookies/invalid-value

    Check server logs for:
        [django-bolt] WARNING: Cookie 'injection' value contains control characters - rejected for security

    The Set-Cookie header will NOT appear in the response.
    """
    from django_bolt.responses import JSON

    response = JSON(
        {
            "message": "Attempted to set cookie with control characters in value",
            "cookie_name": "injection",
            "note": "This cookie will be rejected by Rust validation - check server logs",
        }
    )
    # This value contains CRLF which could enable header injection
    # Rust will reject it for security
    response.set_cookie("injection", "value\r\nSet-Cookie: evil=1", httponly=True)
    return response


@api.get("/cookies/mixed")
async def cookies_mixed():
    """
    Set multiple cookies - some valid, some invalid.

    Demonstrates that invalid cookies are rejected individually,
    while valid cookies in the same response are still set.

    Test with:
        curl -v http://localhost:8000/cookies/mixed

    Only valid cookies will appear in Set-Cookie headers.
    Invalid ones will be logged and skipped.
    """
    from django_bolt.responses import JSON

    response = JSON(
        {
            "message": "Mixed valid/invalid cookies",
            "valid_cookies": ["good_session", "user_prefs"],
            "invalid_cookies": ["bad; name", "has_control_chars"],
        }
    )

    # Valid cookies - these will be set
    response.set_cookie("good_session", "valid123", httponly=True, secure=True)
    response.set_cookie("user_prefs", "theme=dark", max_age=86400)

    # Invalid cookie name - will be rejected
    response.set_cookie("bad; name", "value")

    # Invalid cookie value (control characters) - will be rejected
    response.set_cookie("has_control_chars", "line1\nline2")

    return response


@api.get("/cookies/special-chars")
async def cookies_special_chars():
    """
    Set a cookie with special characters in the value that need escaping.

    The cookie crate handles proper quoting/escaping for RFC 6265 compliance.

    Test with:
        curl -v http://localhost:8000/cookies/special-chars

    The value with spaces will be properly quoted in the Set-Cookie header.
    """
    from django_bolt.responses import JSON

    response = JSON(
        {
            "message": "Cookie with special characters",
            "cookie_name": "data",
            "cookie_value": "hello world with spaces",
        }
    )
    # Spaces require quoting per RFC 6265 - the cookie crate handles this
    response.set_cookie("data", "hello world with spaces")
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


@api.post("/mixed-form")
async def handle_mixed(
    title: Annotated[str, Form()],
    description: Annotated[str, Form()],
    attachments: Annotated[list[dict], File(alias="file")] = None,
):
    result = {"title": title, "description": description, "has_attachments": bool(attachments)}
    if attachments:
        result["attachment_count"] = len(attachments)
    return result


# ==== File serving endpoint for benchmarks ====
THIS_FILE = os.path.abspath(__file__)


@api.get("/file-static")
async def file_static():
    return FileResponse(THIS_FILE, filename="api.py")


@api.get("/file-static-nonexistent")
async def file_static_nonexistent():
    return FileResponse("/path/to/nonexistent/file.txt", filename="asdfasd.py")


# ==== Streaming endpoints for benchmarks ====
@api.get("/stream")
@no_compress
async def stream_plain():
    def gen():
        for _i in range(100):
            yield "x"

    return StreamingResponse(gen(), media_type="text/plain")


@api.get("/collected")
async def collected_plain():
    # Same data but collected into a single response
    return PlainText("x" * 100)


@api.get("/sse")
@no_compress
async def sse():
    async def gen():
        while True:
            await asyncio.sleep(1)
            yield f"data: {time.time()}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@api.get("/sync-sse")
@no_compress
def sse_sync():
    """Sync version: Server-Sent Events."""

    def gen():
        while True:
            time.sleep(1)
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


@api.get("/sse-new-explicit")
async def sse_new_explicit():
    """Explicit SSE: return EventSourceResponse with mixed yields."""

    async def gen():
        yield {"message": "stream started", "time": time.time()}
        for i in range(5):
            await asyncio.sleep(1)
            yield ServerSentEvent(
                data={"count": i, "time": time.time()},
                event="tick",
                id=str(i),
            )
        yield ServerSentEvent(comment="stream ending")
        yield ServerSentEvent(data="done", event="complete")

    return EventSourceResponse(gen())


# ==== OpenAI-style Chat Completions (streaming/non-streaming) ====
class ChatMessage(msgspec.Struct):
    role: str
    content: str


class ChatCompletionRequest(msgspec.Struct):
    model: str = "gpt-4o-mini"
    messages: list[ChatMessage] = []
    stream: bool = True
    n_chunks: int = 50
    token: str = " hello"
    delay_ms: int = 0


# Optimized msgspec structs for streaming responses (zero-allocation serialization)
class ChatCompletionChunkDelta(msgspec.Struct):
    content: str | None = None


class ChatCompletionChunkChoice(msgspec.Struct):
    index: int
    delta: ChatCompletionChunkDelta
    finish_reason: str | None = None


class ChatCompletionChunk(msgspec.Struct):
    id: str
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
    object: str = "chat.completion.chunk"


@api.get("/v1/chat/completions")
@no_compress
async def openai_chat_completions():
    """Ultra-optimized chat completions endpoint that streams 100 chunks using msgspec"""
    created = int(time.time())
    model = "gpt-4o-mini"
    chat_id = "chatcmpl-bolt-bench"

    # Pre-create reusable msgspec structs (minimal object creation)
    stop_delta = ChatCompletionChunkDelta()

    async def agen():
        # Ultra-optimized: reuse structs and minimize allocations
        for i in range(50):
            await asyncio.sleep(0.2)
            # Reuse pre-created delta struct
            choice = ChatCompletionChunkChoice(
                index=0, delta=ChatCompletionChunkDelta(content=f"hello - {i}"), finish_reason=None
            )
            chunk = ChatCompletionChunk(id=chat_id, created=created, model=model, choices=[choice])

            # msgspec.json.encode directly to bytes - fastest possible path
            chunk_bytes = msgspec.json.encode(chunk)
            yield b"data: " + chunk_bytes + b"\n\n"

        # Final chunk with stop reason
        final_choice = ChatCompletionChunkChoice(index=0, delta=stop_delta, finish_reason="stop")
        final_chunk = ChatCompletionChunk(id=chat_id, created=created, model=model, choices=[final_choice])
        final_bytes = msgspec.json.encode(final_chunk)
        yield b"data: " + final_bytes + b"\n\n"
        yield b"data: [DONE]\n\n"

    return StreamingResponse(agen(), media_type="text/event-stream")


# ==== Error Handling & Logging Examples ====


# Example 1: Using specialized HTTP exceptions
@api.get("/errors/not-found/{resource_id}")
async def error_not_found(resource_id: int):
    """Example of NotFound exception with custom message."""
    if resource_id == 0:
        raise NotFound(detail=f"Resource {resource_id} not found")
    return {"resource_id": resource_id, "status": "found"}


@api.get("/errors/bad-request")
async def error_bad_request(value: int | None = None):
    """Example of BadRequest exception."""
    if value is None or value < 0:
        raise BadRequest(detail="Value must be a positive integer")
    return {"value": value, "doubled": value * 2}


@api.get("/errors/unauthorized")
async def error_unauthorized():
    """Example of Unauthorized exception with headers."""
    raise Unauthorized(detail="Authentication required", headers={"WWW-Authenticate": 'Bearer realm="API"'})


# Example 2: Validation errors with field-level details
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
@api.get("/errors/internal")
async def error_internal():
    """Example of generic exception that triggers debug mode behavior.

    In DEBUG=True: Returns 500 with full traceback
    In DEBUG=False: Returns 500 with generic message
    """
    # This simulates an unexpected error
    result = 1 / 0  # ZeroDivisionError
    return {"result": result}


# Example 4: Custom error with extra data
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


# ==== Compression Test Endpoint ====
@api.get("/compression-test")
# @no_compress
async def compression_test():
    """
    Endpoint to test compression.

    Returns a large JSON response (>1KB) that should be compressed
    when client sends Accept-Encoding: gzip, br, deflate headers.

    Test with:
        curl -H "Accept-Encoding: gzip, br" http://localhost:8000/compression-test -v

    Check for "Content-Encoding" header in response.
    """
    # Generate large data (>1KB to trigger compression)
    large_data = {
        "message": "This is a compression test endpoint",
        "compression_info": {
            "enabled": "Compression is enabled by default in Django-Bolt",
            "algorithms": ["brotli", "gzip", "zstd"],
            "automatic": "Actix Web automatically compresses based on Accept-Encoding header",
            "threshold": "Responses larger than ~1KB are compressed",
        },
        "sample_data": [
            {
                "id": i,
                "name": f"Item {i}",
                "description": "This is a sample description that adds to the response size. " * 5,
                "metadata": {
                    "created_at": "2025-01-01T00:00:00Z",
                    "updated_at": "2025-01-02T00:00:00Z",
                    "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
                    "properties": {
                        "key1": "value1",
                        "key2": "value2",
                        "key3": "value3",
                    },
                },
            }
            for i in range(50)  # 50 items to ensure >1KB
        ],
        "instructions": {
            "step1": "Send a request with 'Accept-Encoding: gzip, br' header",
            "step2": "Check response headers for 'Content-Encoding'",
            "step3": "Compare response size with/without compression",
            "note": "Small responses (<1KB) won't be compressed even with Accept-Encoding",
        },
    }

    return large_data


# ============================================================================
# Serializer Benchmark Endpoints
# ============================================================================


class BenchAuthorRaw(msgspec.Struct):
    """Raw msgspec for baseline comparison."""

    id: int
    name: Annotated[str, Meta(min_length=2)]
    email: Annotated[str, Meta(pattern=r"^[^@]+@[^@]+\.[^@]+$")]
    bio: str = ""


class BenchAuthorWithValidators(Serializer):
    """Django-Bolt Serializer with custom field validators."""

    id: int
    name: Annotated[str, Meta(min_length=2)]
    email: Annotated[str, Meta(pattern=r"^[^@]+@[^@]+\.[^@]+$")]
    bio: str = ""

    @field_validator("name")
    def strip_name(cls, value: str) -> str:
        """Strip whitespace from name."""
        return value.strip()

    @field_validator("email")
    def lowercase_email(cls, value: str) -> str:
        """Lowercase email for consistency."""
        return value.lower()

    # @field_validator("password")
    # def validate_password(cls, value: str) -> str:
    #     """Validate password strength."""
    #     if value == "4321":
    #         raise ValidationError("Incorrect password")
    #     # MUST return the value (or transformed value)
    #     return value


@api.post("/bench/serializer-raw")
async def bench_serializer_raw(author: BenchAuthorRaw) -> BenchAuthorRaw:
    """
    Benchmark endpoint using raw msgspec (no validators).
    Tests pure msgspec deserialization and serialization.
    """
    return author


@api.post("/bench/serializer-validated")
async def bench_serializer_validated(author: BenchAuthorWithValidators) -> BenchAuthorWithValidators:
    """
    Benchmark endpoint using Django-Bolt Serializer with custom validators.
    Tests deserialization with field validators (strip, lowercase).

    Validates that:
    - name is stripped of whitespace
    - email is lowercased
    """
    # Ensure validations worked
    assert author.name == author.name.strip(), "Name should be stripped"
    assert author.email == author.email.lower(), "Email should be lowercase"

    return author


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
# Multi-Response Benchmark Endpoints
# ============================================================================


class BenchOk(msgspec.Struct):
    id: int
    name: str


class BenchError(msgspec.Struct):
    detail: str


@api.get("/bench/multi/tuple", response_model={200: BenchOk, 400: BenchError})
async def bench_multi_tuple():
    return (200, {"id": 1, "name": "alice"})


@api.get("/bench/multi/dict", response_model={200: BenchOk, 400: BenchError})
async def bench_multi_dict():
    return {"id": 1, "name": "alice"}


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
# Class-Based Views (ViewSet) - Using Unified ViewSet Pattern with @action
# ============================================================================


# ============================================================================
# Benchmark ViewSets - Using Decorator Syntax
# ============================================================================


@api.view("/cbv-items100")
class Items100ViewSet(ViewSet):
    """ViewSet that returns 100 items (for benchmarking)."""

    async def get(self, request):
        """GET /cbv-items100 - Return 100 items."""
        return [{"name": f"item{i}", "price": float(i), "is_offer": (i % 2 == 0)} for i in range(100)]


@api.view("/cbv-bench-parse")
class BenchParseViewSet(ViewSet):
    """ViewSet for JSON parsing benchmark."""

    async def post(self, request, payload: BenchPayload):
        """POST /cbv-bench-parse - Parse and validate JSON payload."""
        return {"ok": True, "n": len(payload.items), "count": payload.count, "cbv": True}


# ============================================================================
# Response Type ViewSets - Using Decorator Syntax
# ============================================================================


@api.view("/cbv-response")
class ResponseTypeViewSet(ViewSet):
    """ViewSet demonstrating different response types."""

    async def get(self, request, response_type: str = "json"):
        """GET /cbv-response - Return different response types based on parameter."""
        if response_type == "plain":
            return PlainText("Hello from ViewSet")
        elif response_type == "html":
            return HTML("<h1>Hello from ViewSet</h1>")
        elif response_type == "redirect":
            return Redirect("/", status_code=302)
        else:
            return {"type": "json", "message": "Hello from ViewSet"}


@api.view("/cbv-header")
class HeaderViewSet(ViewSet):
    """ViewSet for header extraction."""

    async def get(self, request, x: Annotated[str, Header(alias="x-test")]):
        """GET /cbv-header - Extract custom header."""
        return PlainText(f"Header: {x}")


@api.view("/cbv-cookie")
class CookieViewSet(ViewSet):
    """ViewSet for cookie extraction."""

    async def post(self, request, val: Annotated[str, Cookie(alias="session")]):
        """POST /cbv-cookie - Extract cookie."""
        return PlainText(f"Cookie: {val}")


# ============================================================================
# Streaming ViewSets - Using Decorator Syntax
# ============================================================================


@api.view("/cbv-stream")
class StreamViewSet(ViewSet):
    """ViewSet for streaming responses."""

    @no_compress
    async def get(self, request):
        """GET /cbv-stream - Stream plain text."""

        def gen():
            for _i in range(100):
                yield "x"

        return StreamingResponse(gen(), media_type="text/plain")


@api.view("/cbv-sse")
class SSEViewSet(ViewSet):
    """ViewSet for Server-Sent Events."""

    @no_compress
    async def get(self, request):
        """GET /cbv-sse - Stream SSE events."""

        def gen():
            for i in range(3):
                yield f"data: {i}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")


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
    try:
        async for message in websocket.iter_text():
            await websocket.send_text(message)
    except Exception:
        pass  # Client disconnected


@api.websocket("/ws/echo")
async def websocket_echo(websocket: WebSocket):
    """
    WebSocket echo endpoint.
    """
    await websocket.accept()
    try:
        async for message in websocket.iter_json():
            await websocket.send_text(f"Echo: {message}")
    except Exception as e:
        print(f"Error in websocket_echo: {e}")
        await websocket.close(code=1011, reason="Some Error")


@api.websocket("/ws/room/{room_id}")
async def websocket_room(websocket: WebSocket, room_id: str):
    """
    WebSocket echo endpoint with room ID path parameter.

    Echoes back messages with the room ID prefix.

    Test with:
        websocat ws://localhost:8000/ws/room/lobby
    """
    await websocket.accept()
    try:
        async for message in websocket.iter_text():
            await websocket.send_text(f"[{room_id}] {message}")
    except Exception:
        pass  # Client disconnected


@api.view("/cbv-chat-completions")
class ChatCompletionsViewSet(ViewSet):
    """ViewSet for OpenAI-style chat completions."""

    @no_compress
    async def post(self, request, payload: ChatCompletionRequest):
        """POST /cbv-chat-completions - Handle chat completions with streaming support."""
        created = int(time.time())
        model = payload.model or "gpt-4o-mini"
        chat_id = "chatcmpl-bolt-cbv"

        if payload.stream:

            async def agen():
                delay = max(0, payload.delay_ms or 0) / 1000.0
                for _i in range(max(1, payload.n_chunks)):
                    chunk = ChatCompletionChunk(
                        id=chat_id,
                        created=created,
                        model=model,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0, delta=ChatCompletionChunkDelta(content=payload.token), finish_reason=None
                            )
                        ],
                    )
                    chunk_json = msgspec.json.encode(chunk)
                    yield b"data: " + chunk_json + b"\n\n"

                    if delay > 0:
                        await asyncio.sleep(delay)

                # Final chunk
                final_chunk = ChatCompletionChunk(
                    id=chat_id,
                    created=created,
                    model=model,
                    choices=[
                        ChatCompletionChunkChoice(index=0, delta=ChatCompletionChunkDelta(), finish_reason="stop")
                    ],
                )
                final_json = msgspec.json.encode(final_chunk)
                yield b"data: " + final_json + b"\n\n"
                yield b"data: [DONE]\n\n"

            return StreamingResponse(agen(), media_type="text/event-stream")

        # Non-streaming
        text = (payload.token * max(1, payload.n_chunks)).strip()
        response = {
            "id": chat_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        }
        return response
