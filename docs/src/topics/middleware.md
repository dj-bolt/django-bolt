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
@api.get("/api/data")
@cors(
    origins=["https://example.com", "https://app.example.com"],
    methods=["GET", "POST", "PUT", "DELETE"],
    headers=["Content-Type", "Authorization"],
    credentials=True,
    max_age=3600,  # Preflight cache duration
)
async def get_data():
    return {"data": "value"}
```

The `origins` and `origin_regexes` of a route are its complete allow list. The route does not use `CORS_ALLOWED_ORIGINS` or `CORS_ALLOWED_ORIGIN_REGEXES`. Thus a route can allow fewer origins than the global settings. Use `origin_regexes` to allow a pattern of origins on one route:

```python
@api.get("/api/tenant")
@cors(origin_regexes=[r"^https://[a-z]+\.tenant\.example\.com$"], credentials=True)
async def tenant_data():
    return {"data": "value"}
```

A pattern can match a part of the origin, so start it with `^` and end it with `$`. Patterns use the syntax of the Rust `regex` crate, which has no look-around and no back-references. An invalid pattern or an empty pattern stops startup with `ValueError`. Bolt does not reject a pattern that allows too much. A pattern such as `.*` or `^` allows every origin.

The most specific `cors` config applies. A route-level `@cors` takes precedence over the router, and the router takes precedence over the API. A config with no origins and no patterns, such as a router config that sets only `credentials`, keeps the allow list of the outer config. A route takes one cors config. A second `@cors`, or a `@cors` together with `@middleware(type="cors")`, raises `ValueError`. The config stays on the function, so give each route with `@cors` its own function. `@cors` raises `ValueError` when it gets no origin and no pattern. It raises `TypeError` when an origin or a pattern is not a `str`. Give `methods` and `headers` as a list of `str`, `credentials` as a `bool`, and `max_age` as an `int` of 0 or more. Another type raises `TypeError`, and a `max_age` out of range raises `ValueError`. A `cors` dict gets the same checks when you register it. This applies to `@middleware(type="cors", ...)` and to a dict in the `middleware` of an API or a router. Only the config of a route needs an origin or a pattern. A method name or a header name with a character that is not valid in an HTTP header, such as a line break, stops startup with `ValueError`.

A WebSocket route does not use `@cors`. The WebSocket handshake checks the `Origin` header against the global settings only.

### Global CORS

Configure CORS for all endpoints in `settings.py`:

```python
# Allow specific origins
CORS_ALLOWED_ORIGINS = [
    "https://example.com",
    "https://app.example.com",
]

# Allow each origin that matches a pattern
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://\w+\.example\.com$",
]

# Allow all origins (development only!)
CORS_ALLOW_ALL_ORIGINS = True

# Additional settings
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
CORS_ALLOW_HEADERS = ["Content-Type", "Authorization", "X-Requested-With"]
CORS_EXPOSE_HEADERS = ["X-Total-Count", "X-Page-Count"]
CORS_PREFLIGHT_MAX_AGE = 86400  # 24 hours
```

Without these settings, Bolt allows the methods `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` and `QUERY`, and the headers `Content-Type` and `Authorization`. A preflight answer is cached for 3600 seconds. A `"*"` entry in `CORS_ALLOWED_ORIGINS` is not a wildcard. Use `CORS_ALLOW_ALL_ORIGINS = True` to allow each origin. A pattern in `CORS_ALLOWED_ORIGIN_REGEXES` can match a part of the origin, so start it with `^` and end it with `$`. An invalid pattern or an empty pattern stops startup with `ImproperlyConfigured`. A value in `CORS_ALLOW_METHODS`, `CORS_ALLOW_HEADERS` or `CORS_EXPOSE_HEADERS` that is not valid in an HTTP header does the same.

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

The most specific rate limit applies. A `@rate_limit` of the route takes precedence over a `rate_limit` dict in the `middleware` of the router, and the router takes precedence over the API. The limit of the outer level does not also apply. `@skip_middleware("rate_limit")` removes the limit of each level.

A route takes one rate limit. A second `@rate_limit`, or a `@rate_limit` together with `@middleware(type="rate_limit")`, raises `ValueError`. The config stays on the function, so give each route with `@rate_limit` its own function.

### Counting per caller

`key="ip"` counts per client address. An IPv6 client counts per /64 network,
because one client often holds a whole /64. Set
[`BOLT_RATE_LIMIT_IPV6_PREFIX`](../ref/settings.md#bolt_rate_limit_ipv6_prefix)
to change the prefix. A header name counts per header value.
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

A name selects a Rust middleware: `cors`, `rate_limit` or `compression`. A name
also selects a Python middleware by the name or the dotted path of its class or
function. A name ignores the letter case. You can also give the middleware class
itself, for example `@skip_middleware(TimingMiddleware)`. A value that is not a
`str` or a class raises `TypeError`. `*` skips each Bolt middleware: the Python middleware and the Rust
middleware. `*` does not skip a Django middleware. To skip a Django middleware,
give the name of its class, for example `@skip_middleware("CsrfViewMiddleware")`.
Thus `*` cannot remove the CSRF check of a route.

Or skip each Bolt middleware:

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

### Answer without the handler

A middleware can answer the request itself. Return a response and do not call `get_response`:

```python
from django_bolt.middleware import Middleware
from django_bolt.responses import JSON


class MaintenanceMiddleware(Middleware):
    async def process_request(self, request):
        if request.headers.get("x-maintenance") == "1":
            return JSON({"detail": "maintenance"}, status_code=503, headers={"Retry-After": "60"})
        return await self.get_response(request)
```

A middleware can return what a handler can return, for example a response object such as `JSON`, `PlainText` or `Redirect`, or a dict. Bolt serializes the value as it serializes the return of the handler of the route. Thus a dict gets the status code of the route, and a `response_model` of the route validates it. A response object with its own status code is the clear form. `raise HTTPException(...)` also stops the request.

An outer middleware gets the object that the inner middleware returned. A response object has `status_code` and `headers`. A dict has none of them, so return a response object when an outer middleware changes the response.

A tuple from a middleware is the internal wire tuple of Bolt, not `(status, data)`.

### Allowed middleware entries

- Middleware classes (`__init__(get_response)` and `__call__(request)`)
- `DjangoMiddleware(...)` wrappers
- `DjangoMiddlewareStack(...)` wrappers
- Dict middleware configs for Rust-handled middleware (`cors`, `rate_limit`)

Passing plain middleware instances in these lists fails fast with `TypeError` (`pass class, not instance`).

A class with no `__call__` method, for example `int`, also raises `TypeError`. Bolt calls the instance with the request.

A dict config needs `"type": "cors"` or `"type": "rate_limit"`. A dict with no type or with another type raises `ValueError`, because no middleware reads it. `@middleware(type="cors", ...)` is the same dict as keyword arguments. A dict config gets the checks of `@cors(...)` or `@rate_limit(...)`, so a value that the decorator refuses raises the same error. A key that is not a keyword argument of the decorator raises `TypeError`, and the error lists the valid keys. Other keyword arguments of `@middleware` raise `TypeError`. Use `@cors(...)`, `@rate_limit(...)` and `@skip_middleware(...)`.

In a `cors` dict, `origins` and `origin_regexes` take a `str` or a collection of `str`. Another type, or an empty pattern, stops startup with `TypeError` or `ValueError`.

Router middleware is inherited parent-to-child and executes in declared order.

A nested router also takes `auth`, `guards` and `tags` from its parent router. A value that the child router or the route sets wins. The nearest parent that sets a value gives it.

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

Each wrapper runs the `process_view` hook of its middleware before the handler. The hook gets the route handler as the view, so view decorators such as `@csrf_exempt` and `@login_not_required` work.

Put a view decorator below the route decorator. The route decorator registers the function that it gets. A view decorator above it marks a function that Bolt did not register. Thus it has no effect, and Bolt gives no error. The wrapper of `@csrf_exempt` passes `request` to the handler, so the handler must take `request` as its first parameter. Bolt raises a `TypeError` at registration when the handler has no parameter for it.

```python
@api.post("/webhook")   # First: the route decorator
@csrf_exempt            # Second: the view decorator
async def webhook(request):
    return {"ok": True}
```

Bolt runs four hooks of a Django middleware: `process_request`, `process_view`, `process_response` and `process_exception`. It does not run `process_template_response`.

When a handler raises an exception, Bolt calls `process_exception` of each middleware, as Django does. The last middleware runs first. The first response that a hook returns answers the request. If no hook returns a response, the Bolt error handlers make the error response. See [Error Handling](error-handling.md).

An `HTTPException` and a validation error do not go to `process_exception`. They are responses of the API, and Bolt builds them.

Bolt makes the error response inside the middleware chain, as Django does. Thus `process_response` of each middleware gets the error response too. For example, `SessionMiddleware` saves the session, and a middleware that adds a header adds it. This applies to the response of `process_exception`, to an `HTTPException`, to a validation error and to the 500 of an unhandled exception.

`get_response(request)` does not raise the exception of the handler. It returns the error response. To handle the exception in a middleware, use `process_exception`. A Bolt middleware around a Django middleware gets the error response from `call_next` too.

The Django middleware also gets a response that Rust gives before the handler. See [Responses from Rust](#responses-from-rust).

A list of wrappers runs the hooks in a different order from Django. Each wrapper runs its `process_request` and then its `process_view`, before the next wrapper starts. Django runs all `process_request` hooks first, and then all `process_view` hooks. If the order of the hooks is important, use `django_middleware=[...]` or `DjangoMiddlewareStack`. They use the order of Django.

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

An entry of the list can also be the dotted path of a class, as for `DjangoMiddleware`.

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
| User (sync) | AuthenticationMiddleware | `request.user` (in async code, see below) |
| Session | SessionMiddleware | `request.session` |
| Messages | MessageMiddleware | `request.state["_messages"]` |
| META | All middleware | `request.state["META"]` |
| CSRF token | CsrfViewMiddleware | `request.state["_csrf_token"]` |
| Custom attributes | Your middleware | `request.state["<name>"]` |

In async code behind Django middleware, `request.user` works. Its query runs on the lane of the request (see [Authentication](authentication.md#accessing-the-authenticated-user)). This includes the session user of `AuthenticationMiddleware`: Bolt runs its query on the lane, where Django alone raises `SynchronousOnlyOperation`. The event loop waits for that query. `await request.auser()` does not block the loop.

Bolt copies each public attribute that a middleware sets on the Django request to `request.state`. For example, django-tenants sets `request.tenant`. Read it from `request.state["tenant"]`. Bolt does not copy names that start with `_`.

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

- **Sync handler:** one lane runs the complete request: the middleware, the handler, and the serializer. The request uses no asyncio.
- **Async handler:** the handler body runs on the event loop. The middleware hooks, ORM calls, and `sync_to_thread` calls of that request all go to one lane.

A middleware class with an async `__call__`, its own `__acall__`, or `sync_capable = False` needs the event loop. Python route middleware and async dependencies need it too. The middleware then runs as in an async request, and a sync handler still runs on the lane of its request.

On a lane, `DjangoMiddleware` uses a second instance of your middleware class in sync mode, as Django does under WSGI. Thus `__init__` runs two times, and the two instances do not share attributes.

In an async request, Bolt moves work to the lane one time for all `process_request` and `process_view` hooks. It moves work one more time for all `process_response` hooks. A stack with only Django built-in hooks runs them on the event loop.

Lanes need no configuration. A request takes an idle lane, or Bolt starts a new one. A lane that stays idle for 10 seconds closes its database connections and stops. [`DJANGO_BOLT_LANE_IDLE_SECONDS`](../ref/settings.md#django_bolt_lane_idle_seconds) changes this time.

When `runbolt` stops, each lane closes its database connections and stops before Python exits. Bolt waits up to 2 seconds for a lane that still runs a call.

#### Database connections

A lane keeps its database connections open between requests. This is different from Django, which closes a connection after each request when `CONN_MAX_AGE = 0`. Bolt applies `CONN_MAX_AGE` and `CONN_HEALTH_CHECKS` on lanes when you set them.

At peak load, the lane count equals the count of concurrent requests that run sync code. Each lane holds one connection for each database that it used. Set `max_connections` of the database for your peak concurrency, or use a pooler such as PgBouncer.

### Performance notes

Django-Bolt optimizes middleware execution with a three-tier system:

1. **Django built-in middleware** - Executed directly without thread pool overhead (fastest)
2. **Third-party middleware with hooks** - Wrapped in `sync_to_async` for safety
3. **Custom `__call__` middleware** - Executed as a chain via single `sync_to_async` call

A class with hook methods is in tier 1 or 2, also when it defines its own `__call__`. `DjangoMiddlewareStack` runs its hooks and does not call its `__call__`. Use `DjangoMiddleware` for a class whose `__call__` must run.

The `DjangoMiddlewareStack` automatically categorizes your middleware for optimal performance.

If a `DjangoMiddlewareStack` mixes hook middleware (`process_request` / `process_view` / `process_response`) with `__call__`-only middleware, Django-Bolt uses a correctness-first compatibility path to preserve strict declared order and hook semantics. This path is slower than the pure hook fast path.

## Position of the route decorators

Put `@cors`, `@rate_limit`, `@middleware`, `@skip_middleware`, `@no_compress` and `@paginate` below the route decorator:

```python
@api.get("/limited")
@rate_limit(rps=10)
async def limited():
    return {"ok": True}
```

The route decorator reads the configuration of the handler when it registers the route. A decorator above it has no effect, so Bolt raises `TypeError` at startup. A `Router` registers its routes in `include_router()`, so a decorator above `@router.get(...)` applies.

## Middleware order

Python middleware execution order is explicit and strict:

1. Global middleware (`BoltAPI(middleware=[...])`)
2. Router middleware (parent router to child router)
3. Route middleware (`@middleware(...)` / function-style `@middleware`)
4. Handler

One `@middleware(A, B)` runs `A` first and then `B`. When a route has more than one `@middleware` decorator, the decorator nearest to the function runs first. Thus `@middleware(A)` above `@middleware(B)` runs `B` first and then `A`.

For responses, the order is reversed.

`BoltAPI(django_middleware=...)` puts the Django middleware before the entries of `middleware=[...]`. A `DjangoMiddleware` or `DjangoMiddlewareStack` in `middleware=[...]` runs at its position in the list.

A Python middleware before the Django middleware runs before any Django hook, as in Django's `MIDDLEWARE` order. It does not run on the lane of the request. It does not see the thread-local state of the Django middleware, for example the tenant of django-tenants. If it loads `request.user`, it loads the user without that state, and the handler then gets the same user. Put a middleware that reads the user after the Django middleware.

Rust-handled middleware configs (for example `@cors` and `@rate_limit`) are still compiled from metadata and executed in Rust.

### Responses from Rust

Rust gives some responses before the handler runs:

- a 404 for a path with no route, and a 405 for a method that the path does not have
- a 401 or a 403 from authentication, a guard or the CSRF check of a cookie
- a 429 from a rate limit

Django runs each middleware for such a request. Bolt does the same. The Python middleware and the Django middleware get the response of Rust as the response of the handler. Thus a request-id middleware adds its header to a 404, and a tenant middleware sets the tenant for it. The handler does not run.

- A request with no route runs the middleware of the API. It does not run router or route middleware, because it has no route. When `runbolt` serves more than one API, the request gets the middleware of the first API.
- A rejection of a route runs all the middleware of the route: the API, router and route middleware.
- `process_view` runs for a rejection, because the route has a view. It does not run for a request with no route, as in Django.
- Bolt does not read the body of these requests. `request.body` is empty.
- With an `error_formatter`, the middleware gets the formatted response.

Rust still does the check without the GIL. An API with no Python middleware and no Django middleware answers these requests in Rust, as before. A route with no Python middleware also answers its rejections in Rust. Thus a flood of 404 or 429 responses costs no Python time when no middleware must see it.

Other early responses of Rust do not go through the middleware. Examples are a 422 or a 400 from request validation and a 413 for a large body. The 308 redirect to the other slash form and the automatic `OPTIONS` answer also do not.

## Performance

Django-Bolt's middleware runs in Rust where possible:

- CORS preflight handling
- Rate limiting with token bucket
- Response compression

This means these operations don't acquire the Python GIL, enabling higher throughput.
