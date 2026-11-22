---
description: Run code before and after each handler. Write Bolt middleware, apply it to an API, a router or a route, and run Django middleware.
---

# Middleware

Middleware is code that runs around a handler. Use it for work that many routes share, such as a request ID, a header or a maintenance switch.

Django-Bolt has three kinds of middleware:

| Kind | Runs in | Examples | Configure with |
| --- | --- | --- | --- |
| Built-in Rust middleware | Rust, with no Python | Authentication, guards, [CORS](cors.md), [rate limiting](rate-limiting.md), [compression](compression.md) | Decorators and settings |
| Bolt middleware | Python, on the event loop | Your classes and functions | `middleware=[...]`, `@middleware` |
| Django middleware | Python, through an adapter | `SessionMiddleware`, `CsrfViewMiddleware`, django-tenants | `django_middleware=...`, `DjangoMiddleware` |

Bolt runs no Python middleware by default. Each Python middleware that you add costs time on each request of its routes.

## Write a middleware

Subclass `Middleware` and define `process_request`. Call `self.get_response(request)` to run the rest of the chain and the handler:

```python title="blog/middleware.py"
import uuid

from django_bolt import Request
from django_bolt.middleware import Middleware


class RequestIdMiddleware(Middleware):
    async def process_request(self, request: Request):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state["request_id"] = request_id
        response = await self.get_response(request)
        response.headers["X-Request-ID"] = request_id
        return response
```

Pass the class, not an instance, to the API:

```python title="blog/api.py"
from django_bolt import BoltAPI

api = BoltAPI(middleware=[RequestIdMiddleware])


@api.get("/articles")
async def list_articles(request: Request) -> dict:
    return {"request_id": request.state["request_id"]}
```

```bash
curl -i localhost:8000/articles -H "X-Request-ID: abc123"
```

```http
HTTP/1.1 200 OK
x-request-id: abc123
content-type: application/json

{"request_id":"abc123"}
```

A middleware gets the Bolt `Request`. `request.headers` has lower-case names. `request.state` is a dict that the handler can read.
`get_response()` gives a response object with `status_code` and `headers`, also when the handler returned a dict.

Bolt reuses one instance of the class for many requests. Do not keep request data on `self`.
An instance in the list, such as `RequestIdMiddleware()`, raises `TypeError`.

## Answer without the handler

Return a response and do not call `get_response()`. `BaseMiddleware` adds `exclude_paths` and `exclude_methods`:

```python
from django_bolt.middleware import BaseMiddleware
from django_bolt.responses import JSON


class MaintenanceMiddleware(BaseMiddleware):
    exclude_paths = ["/health", "/admin/*"]

    async def process_request(self, request: Request):
        if request.headers.get("x-maintenance") == "1":
            return JSON({"detail": "Down for maintenance"}, status_code=503, headers={"Retry-After": "60"})
        return await self.get_response(request)


api = BoltAPI(middleware=[RequestIdMiddleware, MaintenanceMiddleware])


@api.get("/articles")
async def list_articles(request: Request) -> dict:
    return {"request_id": request.state["request_id"]}


@api.get("/health")
async def health() -> dict:
    return {"status": "ok"}
```

| Request | Response |
| --- | --- |
| `GET /articles` with `x-maintenance: 1` | 503 `{"detail": "Down for maintenance"}`, with `retry-after: 60` and `x-request-id` |
| `GET /health` with `x-maintenance: 1` | 200 `{"status": "ok"}` |

In `exclude_paths`, `*` matches one path segment and `**` matches any part of the path. `exclude_methods` takes method names, such as `["OPTIONS"]`.

A middleware can return what a handler returns. A dict gets the status code of the route.
Return a response object, such as `JSON`, when an outer middleware reads `status_code` or `headers`.
`raise HTTPException(...)` also stops the request. See [Error handling](error-handling.md).

## Apply middleware to one route or a router

`@middleware` adds middleware classes to one route. Put it below the route decorator:

```python
from django_bolt.middleware import middleware

api = BoltAPI()


@api.post("/articles")
@middleware(RequestIdMiddleware, MaintenanceMiddleware)
async def create_article(request: Request) -> dict:
    return {"request_id": request.state["request_id"]}
```

`@middleware` on a function `(request, call_next)` makes a route decorator:

```python
import time


@middleware
async def server_timing(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - start) * 1000:.1f}"
    return response


@api.get("/articles/{article_id}")
@server_timing
async def get_article(article_id: int) -> dict:
    return {"id": article_id}
```

A `Router` takes a list of middleware classes. Each route of the router, and of its child routers, runs them:

```python
from django_bolt import Router

admin = Router(prefix="/admin", middleware=[RequestIdMiddleware])


@admin.get("/stats")
async def admin_stats(request: Request) -> dict:
    return {"request_id": request.state["request_id"]}


api.include_router(admin)
```

A function middleware works on a route only. The API and a router take classes, Django middleware wrappers and the dicts of [CORS](cors.md) and [rate limiting](rate-limiting.md).

## Know the order

The middleware of a request runs in this order:

1. The Django middleware of `BoltAPI(django_middleware=...)`.
2. The entries of `BoltAPI(middleware=[...])`, in list order.
3. The middleware of the router, from the parent router to the child router.
4. The middleware of the route. With more than one `@middleware`, the decorator nearest to the function runs first. In `@middleware(A, B)`, `A` runs first.
5. The handler.

The response goes back in the reverse order.

Rust runs authentication, guards and rate limits before all Python middleware.
A request that Rust rejects, for example with a 401 or a 429, does not reach Python middleware. A request that matches no route does not reach it either.
Thus a logging middleware does not see a 404 for an unknown path. Record such requests at your proxy.

The config decorators `@middleware`, `@skip_middleware`, `@no_compress`, `@cors` and `@rate_limit` go below the route decorator.
A config decorator above the route decorator has no effect, so Bolt raises `TypeError` at startup.

## Skip a middleware on a route

`@skip_middleware` removes middleware from one route:

```python
from django_bolt.middleware import skip_middleware


@api.get("/ping")
@skip_middleware(RequestIdMiddleware, "compression")
async def ping() -> dict:
    return {"pong": True}
```

A name can be:

- `"cors"`, `"rate_limit"` or `"compression"` for a built-in Rust middleware.
- The class, the class name or the dotted path of a Python middleware. Letter case does not matter.
- `"*"` for each Bolt middleware, Python and Rust.

`"*"` does not skip a Django middleware, so it cannot remove the CSRF check of Django. Name a Django middleware to skip it, for example `"CsrfViewMiddleware"`.
`@skip_middleware` does not skip authentication or guards. Use `guards=[AllowAny()]` and `auth=[]` for that.

## Run Django middleware

`django_middleware` runs Django middleware on each route of the API:

<!-- fragment -->
```python
api = BoltAPI(django_middleware=True)  # Each entry of settings.MIDDLEWARE, in order.
api = BoltAPI(django_middleware=[      # These classes, in this order.
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
])
api = BoltAPI(django_middleware={"exclude": ["django.middleware.csrf.CsrfViewMiddleware"]})
api = BoltAPI(django_middleware={"include": ["django.contrib.sessions.middleware.SessionMiddleware"]})
```

`include` and `exclude` filter `settings.MIDDLEWARE` and keep its order. A list does not read `settings.MIDDLEWARE`.
A dotted path that does not import stops startup.

`django_middleware=True` includes `CsrfViewMiddleware` when `settings.MIDDLEWARE` has it. Then each `POST` with no CSRF token gets Django's 403.
A route for a client that is not a browser, such as a webhook, can use Django's `@csrf_exempt`. Put it below the route decorator:

```python
from django.views.decorators.csrf import csrf_exempt

api = BoltAPI(django_middleware=True)


@api.get("/whoami")
async def whoami(request: Request) -> dict:
    user = await request.auser()
    return {"authenticated": user.is_authenticated}


@api.post("/webhooks/payment")
@csrf_exempt
async def payment_webhook(request: Request) -> dict:
    return {"received": True}
```

Bolt runs the hooks `process_request`, `process_view`, `process_response` and `process_exception` of a Django middleware. It does not run `process_template_response`.
`process_view` gets the handler as the view, so view decorators such as `@csrf_exempt` and `@login_not_required` work.

### Read what a Django middleware sets

| Django attribute | In a handler |
| --- | --- |
| The user of `AuthenticationMiddleware` | `await request.auser()`, or `request.user` |
| The session of `SessionMiddleware` | `request.session`. Use `await request.session.aget(...)` in async code. |
| Another public attribute, such as `request.tenant` | `request.state["tenant"]` |

Bolt copies each attribute that a middleware sets on the Django request to `request.state`. It does not copy a name that starts with `_`.

### Wrap one Django middleware

`DjangoMiddleware` and `DjangoMiddlewareStack` put Django middleware in a `middleware` list. Use them to run Django middleware on one router:

```python
from django_bolt.middleware import DjangoMiddlewareStack

account = Router(
    prefix="/account",
    middleware=[
        DjangoMiddlewareStack([
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ])
    ],
)


@account.get("/visits")
async def visits(request: Request) -> dict:
    count = await request.session.aget("visits", 0) + 1
    await request.session.aset("visits", count)
    return {"visits": count}


api.include_router(account)
```

The first request to `/account/visits` gives `{"visits": 1}` and a session cookie. The next request with the cookie gives `{"visits": 2}`.
The other routes of the API run no Django middleware.

`DjangoMiddlewareStack` runs its classes with the hook order of Django. `DjangoMiddleware(cls)` wraps one class.
A list of `DjangoMiddleware` runs `process_request` and `process_view` of each class before the next class starts, which is not the order of Django.
Use `DjangoMiddlewareStack` for more than one class.

### Know the cost of Django middleware

A route with Django middleware does more work for each request:

- Bolt builds a Django `HttpRequest` and converts the response back.
- Each hook runs in Python. Some hooks read the database, for example to load the session and the user.
- A sync handler runs on a request thread, not in the fast sync path.

In the Bolt benchmark, a route with the session, auth and messages middleware, a custom middleware and a template served 5,996 requests per second.
A route that read a JWT with no middleware served 155,104 (8 processes, 100 connections, September 2026). See [Benchmarks](../benchmarks.md).

Run Django middleware only on the routes that need it. Use `DjangoMiddlewareStack` on a router, as above.
For login, [`SessionAuthentication`](authentication.md#authenticate-with-a-django-session) adds the session middleware to its routes only.

### Keep thread-local state with the request

Some libraries keep request state in a thread-local value, for example django-tenants on the database connection.
Bolt runs the Django middleware and the sync code of such a request on one thread, which Bolt calls a request lane:

- A sync handler runs on the lane, with the middleware. It sees the state that the middleware set.
- The body of an `async def` handler runs on the event loop thread. There, a thread-local value is not set.
  Read the value from `request.state`, for example `request.state["tenant"]`.
- An ORM call from an async handler, such as `await Article.objects.aget(...)`, runs on the lane, so it uses the correct schema.

A lane keeps its database connections between requests, as a WSGI worker thread does. It stops after 10 seconds with no request.
`DJANGO_BOLT_LANE_IDLE_SECONDS` changes that time. See the [settings reference](../ref/settings.md) and [How it works](../architecture.md).

## Use the built-in Python middleware

`django_bolt.middleware` has three middleware classes:

| Class | Effect |
| --- | --- |
| `TimingMiddleware` | Sets `request.state["request_id"]` and `request.state["start_time"]`. Adds `X-Request-ID` and `X-Response-Time`. |
| `LoggingMiddleware` | Logs each request and response to the `django_bolt.requests` logger. |
| `ErrorHandlerMiddleware` | Logs an unhandled exception and answers 500. |

Bolt already writes an access log in Rust. See [Logging](logging.md).

## See also

- [Middleware reference](../ref/middleware.md): every class, decorator and argument.
- [CORS](cors.md), [Rate limiting](rate-limiting.md) and [Compression](compression.md): the built-in Rust middleware.
- [Authentication](authentication.md): session login with Django's middleware.
- [Dependencies](dependencies.md): shared code for one handler argument, with no middleware.
