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
- `key` - What to count per: `"ip"` (the default), `"user"`, `"api_key"` or a request header name

### Counting per caller

`key="ip"` counts per client address. A header name counts per header value.
Callers that do not send the header share one bucket. Bolt hashes the value,
so its length is not limited.

`key="user"` counts per authenticated identity. `key="api_key"` counts per
authenticated API key. Both run after authentication:

```python
@api.get("/api/data", auth=[JWTAuthentication()])
@rate_limit(rps=10, key="user")
async def data():
    return {"data": []}
```

A caller with no identity is counted per client address. A guard such as
`IsAuthenticated` rejects that caller before the limit is counted. Both keys
need an auth backend on the route or the API. Without one, the server does
not start.

### Client address behind a proxy

With `key="ip"`, Bolt keys on the peer address of the connection. A client sends
`X-Forwarded-For` itself, so Bolt ignores that header by default.

Declare your proxies to make the header trustworthy:

```python
# settings.py
BOLT_TRUSTED_PROXIES = ["10.0.0.0/8"]
```

Bolt then reads `X-Forwarded-For` from the right and takes the first entry that
is not a listed proxy. A client cannot pick its own bucket, because the proxy
appends the real address to the right of anything the client sent.

The resolved address is also exposed as `request.META["REMOTE_ADDR"]`. If any
forwarding hop is malformed, Bolt ignores the chain and uses the connection
peer rather than searching farther left through client-controlled input.

Set this whenever a proxy, a load balancer or a CDN is in front of Bolt.
Without it every caller behind that proxy shares one bucket. See
[BOLT_TRUSTED_PROXIES](../ref/settings.md#bolt_trusted_proxies).

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

Django-Bolt compresses both buffered and streaming responses on by
default (brotli with gzip fallback). Opt out per-route with
`@no_compress`, or tune the backend, levels, and per-stream memory via
`CompressionConfig`:

```python
from django_bolt import BoltAPI
from django_bolt.middleware import CompressionConfig, no_compress

api = BoltAPI(compression=CompressionConfig(backend="brotli"))

@api.get("/raw")
@no_compress
async def raw():
    return {"plain": True}
```

See [Compression](compression.md) for the full configuration, per-chunk
streaming flush behavior, `lgwin` memory tradeoffs, and CRIME/BREACH
guidance.

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

# Load exactly these, in this order (they do not need to be in settings.MIDDLEWARE)
api = BoltAPI(django_middleware=[
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
])

# Load settings.MIDDLEWARE without these
api = BoltAPI(django_middleware={
    "exclude": ["django.middleware.csrf.CsrfViewMiddleware"]
})

# Load only these entries of settings.MIDDLEWARE
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
| Custom attributes | Your middleware | `request.state["<name>"]` |

Bolt copies each public attribute that a middleware sets on the Django request to `request.state`. For example, django-tenants sets `request.tenant`, thus read `request.state["tenant"]`. Bolt does not copy names that start with `_`.

### Thread-local state and django-tenants

Some libraries keep request state on the thread-local database connection. For example, django-tenants stores the tenant in `connection.tenant` and `connection.schema_name`.

Bolt gives each request with Django middleware one request thread, which Bolt calls a lane. Django's ASGI handler has the same rule. These parts run on the lane and read the correct state:

- The Django middleware.
- Sync handlers.
- ORM calls from async handlers, such as `await Item.objects.aget()` and a returned QuerySet.
- Code that you pass to `sync_to_thread` or `sync_to_async`.

The body of an `async def` handler runs on the event loop thread. That thread has a different connection, which the middleware did not configure. In the handler body, `connection.schema_name` reads `public`. Django ASGI async views have the same behavior.

These django-tenants features read the connection on the calling thread. In an async handler body they use the `public` tenant:

- The tenant cache key function (`django_tenants.cache.make_key`). Tenants then share cache entries.
- `TenantFileSystemStorage`.
- The tenant template loaders.
- The tenant log filter.

Obey these rules with django-tenants:

1. Use sync handlers when the handler uses the cache, file storage, or templates.
2. In an async handler, read the tenant from `request.state["tenant"]`.
3. In an async handler, call tenant-aware code through `sync_to_thread`.

```python
from django.core.cache import cache
from django_bolt.concurrency import sync_to_thread

@api.get("/report")
async def report(request: Request):
    tenant = request.state["tenant"]                  # correct on any thread
    cached = await sync_to_thread(cache.get, "report")  # tenant cache key
    return {"tenant": tenant.schema_name, "cached": cached}
```

### Request lanes

A lane is a thread that Rust owns. One lane serves one request at a time, so thread-local state cannot mix between concurrent requests. A lane stays alive between requests and keeps its database connections open, as a WSGI worker thread does.

- **Sync handler:** one lane runs the complete request: the middleware, the handler, and the serializer. The request uses no asyncio. This path applies when all global middleware of the API is `DjangoMiddlewareStack` or `DjangoMiddleware`, and each middleware class can run in sync mode. A class with an async `__call__`, its own `__acall__`, or `sync_capable = False` cannot. For `DjangoMiddleware`, the lane uses a second middleware instance in sync mode, as Django does under WSGI. The route must have no async dependency, no Python route middleware, and no token revocation handler. A sync handler on a route that does not qualify still runs on the lane of its request.
- **Async handler:** the handler body runs on the event loop. The middleware hooks, ORM calls, and `sync_to_thread` calls of that request all go to one lane.

In an async request, one thread hop runs all `process_request` and `process_view` hooks, and one more runs all `process_response` hooks. A stack with only Django built-in hooks needs no hop.

Lanes need no configuration. A request takes an idle lane, or Bolt starts a new one. A lane that stays idle for 10 seconds closes its database connections and stops. [`DJANGO_BOLT_LANE_IDLE_SECONDS`](../ref/settings.md#django_bolt_lane_idle_seconds) changes this time. At peak load, the lane count equals the count of concurrent requests that run sync code. Each lane holds one connection for each database that it used, so set `max_connections` of the database, or use a pooler such as PgBouncer, for your peak concurrency.

Bolt applies `CONN_MAX_AGE` and `CONN_HEALTH_CHECKS` on lanes when you set them. With the default `CONN_MAX_AGE = 0`, a lane keeps its connection until the lane stops.

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
