---
icon: lucide/layers
---

# Middleware

Django-Bolt provides middleware for cross-cutting concerns like CORS and rate limiting. This guide covers the built-in middleware and how to use it.

## CORS middleware

### Per-route CORS

Apply CORS to specific endpoints:

```python
from django_bolt.middleware import cors

@api.get("/api/data")
@cors(origins=["https://example.com"], credentials=True)
async def get_data():
    return {"data": "value"}
```

### CORS options

```python
@cors(
    origins=["https://example.com", "https://app.example.com"],
    methods=["GET", "POST", "PUT", "DELETE"],
    headers=["Content-Type", "Authorization"],
    credentials=True,
    max_age=3600,  # Preflight cache duration
)
```

### Global CORS

Configure CORS for all endpoints in `settings.py`:

```python
# Allow specific origins
CORS_ALLOWED_ORIGINS = [
    "https://example.com",
    "https://app.example.com",
]

# Allow all origins (development only!)
CORS_ALLOW_ALL_ORIGINS = True

# Additional settings
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
CORS_ALLOW_HEADERS = ["Content-Type", "Authorization", "X-Requested-With"]
CORS_EXPOSE_HEADERS = ["X-Total-Count", "X-Page-Count"]
CORS_MAX_AGE = 86400  # 24 hours
```

## Rate limiting

### Per-route rate limiting

```python
from django_bolt.middleware import rate_limit

@api.get("/api/search")
@rate_limit(rps=10, burst=20)
async def search(q: str):
    return {"results": []}
```

Parameters:

- `rps` - Requests per second allowed
- `burst` - Maximum burst size (allows short spikes)

### How it works

Django-Bolt uses a token bucket algorithm:

- Tokens are added at `rps` per second
- Each request consumes one token
- The bucket holds up to `burst` tokens
- If no tokens are available, the request is rejected with 429 Too Many Requests

### Rate limit response

When rate limited, the response includes:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 1
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1640000000
```

## Skipping middleware

Disable specific middleware for an endpoint:

```python
from django_bolt.middleware import skip_middleware

@api.get("/health")
@skip_middleware("cors", "rate_limit")
async def health():
    return {"status": "ok"}
```

Or skip all middleware:

```python
@api.get("/internal")
@skip_middleware("*")
async def internal():
    return {"internal": True}
```

## Compression

Django-Bolt automatically compresses responses. Disable for specific endpoints:

```python
from django_bolt.middleware import no_compress

@api.get("/stream")
@no_compress
async def stream():
    # Streaming responses should not be compressed
    return StreamingResponse(generate())
```

### Compression settings

Configure in `settings.py`:

```python
from django_bolt import CompressionConfig

# In your api.py
api = BoltAPI(
    compression=CompressionConfig(
        backend="brotli",     # "brotli", "gzip", or "zstd"
        minimum_size=1000,    # Only compress responses > 1KB
        gzip_fallback=True,   # Fall back to gzip if client doesn't support brotli
    )
)
```

## Custom middleware

### Function-style middleware (per-route)

Use `@middleware` on a function that accepts `(request, call_next)`:

```python
from django_bolt.middleware import middleware

@middleware
async def timing_middleware(request, call_next):
    request.state["timing_enabled"] = True
    response = await call_next(request)
    response.headers["X-Timing"] = "enabled"
    return response

@api.get("/timed")
@timing_middleware
async def timed_endpoint():
    return {"status": "ok"}
```

In this form, `@middleware` creates a **route decorator** and runs with the `(request, call_next)` contract.

### Class-based middleware (global)

Define a Django-style middleware class and pass the **class** to `BoltAPI(middleware=[...])`:

```python
from django_bolt import BoltAPI
from django_bolt.middleware import Middleware
import uuid


class RequestIdMiddleware(Middleware):
    async def process_request(self, request):
        request_id = str(uuid.uuid4())
        request.state["request_id"] = request_id
        response = await self.get_response(request)
        response.headers["X-Request-ID"] = request_id
        return response


api = BoltAPI(
    middleware=[
        RequestIdMiddleware,  # pass class, not instance
    ]
)
```

### Class-based middleware (per-route)

You can apply class-based middleware to a single route with `@middleware(MyMiddlewareClass)`:

```python
from django_bolt.middleware import middleware

@api.get("/admin-only")
@middleware(RequestIdMiddleware)
async def admin_only():
    return {"ok": True}
```

### Class-based middleware (router-level)

Router middleware applies to all routes in that router (including nested routers):

```python
from django_bolt import Router

admin_router = Router(
    prefix="/admin",
    middleware=[
        RequestIdMiddleware,
    ]
)

@admin_router.get("/users")
async def list_users():
    return {"items": []}

api.include_router(admin_router)
```

### Allowed middleware entries

- Middleware classes (`__init__(get_response)`)
- `DjangoMiddleware(...)` wrappers
- `DjangoMiddlewareStack(...)` wrappers
- Dict middleware configs for Rust-handled middleware (`cors`, `rate_limit`)

Passing plain middleware instances in these lists fails fast with `TypeError` (`pass class, not instance`).

Router middleware is inherited parent-to-child and executes in declared order.

## Django middleware integration

Django-Bolt seamlessly integrates with Django's middleware system, allowing you to use existing Django middleware with your API endpoints.

### Quick start

The simplest approach is to use the `django_middleware` parameter, which loads middleware from your Django `settings.MIDDLEWARE`:

```python
from django_bolt import BoltAPI

# Load all middleware from settings.MIDDLEWARE
api = BoltAPI(django_middleware=True)
```

### Configuration options

The `django_middleware` parameter accepts several configuration formats:

```python
# Load all middleware from settings.MIDDLEWARE
api = BoltAPI(django_middleware=True)

# Disable Django middleware
api = BoltAPI(django_middleware=False)

# Load specific middleware only
api = BoltAPI(django_middleware=[
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
])

# Exclude specific middleware
api = BoltAPI(django_middleware={
    "exclude": ["django.middleware.csrf.CsrfViewMiddleware"]
})

# Include only specific middleware
api = BoltAPI(django_middleware={
    "include": [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
    ]
})
```

### Using DjangoMiddleware wrapper

For wrapping individual middleware classes directly:

```python
from django_bolt import BoltAPI, DjangoMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.auth.middleware import AuthenticationMiddleware

api = BoltAPI(
    middleware=[
        DjangoMiddleware(SessionMiddleware),
        DjangoMiddleware(AuthenticationMiddleware),
    ]
)
```

You can also use import path strings:

```python
api = BoltAPI(
    middleware=[
        DjangoMiddleware("django.contrib.sessions.middleware.SessionMiddleware"),
        DjangoMiddleware("myapp.middleware.CustomMiddleware"),
    ]
)
```

### Using DjangoMiddlewareStack

When using multiple Django middleware, `DjangoMiddlewareStack` is more efficient as it performs a single request conversion instead of one per middleware:

```python
from django_bolt import BoltAPI
from django_bolt.middleware import DjangoMiddlewareStack
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.auth.middleware import AuthenticationMiddleware

api = BoltAPI(
    middleware=[
        DjangoMiddlewareStack([
            SessionMiddleware,
            AuthenticationMiddleware,
        ])
    ]
)
```

### Accessing Django request attributes

Django middleware sets attributes on the request that are automatically synced to the Bolt request:

```python
@api.get("/profile")
async def profile(request):
    # User from AuthenticationMiddleware (async)
    user = await request.auser()

    # Session from SessionMiddleware
    session = request.session
    theme = await session.aget("theme", "light")

    # Messages from MessageMiddleware
    messages = request.state.get("_messages")

    return {
        "username": user.username if user.is_authenticated else "anonymous",
        "authenticated": user.is_authenticated,
        "session_key": session.session_key,
        "theme": theme,
    }
```

Available synced attributes:

| Attribute | Source | Access |
|-----------|--------|--------|
| User (async) | AuthenticationMiddleware | `await request.auser()` |
| User (sync) | AuthenticationMiddleware | `request.user` |
| Session | SessionMiddleware | `request.session` |
| Messages | MessageMiddleware | `request.state["_messages"]` |
| META | All middleware | `request.state["META"]` |
| CSRF token | CsrfViewMiddleware | `request.state["_csrf_token"]` |

### Performance notes

Django-Bolt optimizes middleware execution with a three-tier system:

1. **Django built-in middleware** - Executed directly without thread pool overhead (fastest)
2. **Third-party middleware with hooks** - Wrapped in `sync_to_async` for safety
3. **Custom `__call__` middleware** - Executed as a chain via single `sync_to_async` call

The `DjangoMiddlewareStack` automatically categorizes your middleware for optimal performance.

If a `DjangoMiddlewareStack` mixes hook middleware (`process_request` / `process_view` / `process_response`) with `__call__`-only middleware, Django-Bolt uses a correctness-first compatibility path to preserve strict declared order and hook semantics. This path is slower than the pure hook fast path.

## Middleware order

Python middleware execution order is explicit and strict:

1. Global middleware (`BoltAPI(middleware=[...])`)
2. Router middleware (parent router to child router)
3. Route middleware (`@middleware(...)` / function-style `@middleware`)
4. Handler

For responses, the order is reversed.

Rust-handled middleware configs (for example `@cors` and `@rate_limit`) are still compiled from metadata and executed in Rust.

## Performance

Django-Bolt's middleware runs in Rust where possible:

- CORS preflight handling
- Rate limiting with token bucket
- Response compression

This means these operations don't acquire the Python GIL, enabling higher throughput.
