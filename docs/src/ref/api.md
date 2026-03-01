---
icon: lucide/code
---

# BoltAPI Reference

This page documents the `BoltAPI` class and its methods.

## BoltAPI

The main class for creating Django-Bolt APIs.

```python
from django_bolt import BoltAPI

api = BoltAPI()
```

### Constructor

```python
BoltAPI(
    prefix="",                 # URL prefix for all routes
    trailing_slash="strip",    # Trailing slash handling: "strip", "append", or "keep"
    middleware=None,           # Middleware classes/wrappers (instances rejected)
    django_middleware=None,    # Django middleware configuration
    enable_logging=True,       # Enable request/response logging
    logging_config=None,       # Custom logging configuration
    compression=None,          # CompressionConfig for response compression
    openapi_config=None,       # OpenAPIConfig for documentation
)
```

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prefix` | `str` | `""` | URL prefix applied to all routes (e.g., `/api/v1`) |
| `trailing_slash` | `str` | `"strip"` | How to handle trailing slashes in route paths |
| `middleware` | `list` | `None` | Global middleware entries: classes, `DjangoMiddleware(...)`, `DjangoMiddlewareStack(...)`, or dict middleware configs |
| `django_middleware` | `bool`, `list`, or `dict` | `None` | Django middleware configuration |
| `enable_logging` | `bool` | `True` | Enable request/response logging |
| `logging_config` | `LoggingConfig` | `None` | Custom logging configuration |
| `compression` | `CompressionConfig` | `None` | Response compression settings |
| `openapi_config` | `OpenAPIConfig` | `None` | Configuration for OpenAPI documentation |

`middleware` fails fast for unsupported instances. Pass middleware classes (`pass class, not instance`) for global/router lists.

Python middleware order is: `Global -> Router -> Route -> Handler` (reversed on response).

#### Middleware usage examples

Function-style route middleware:

```python
from django_bolt.middleware import middleware

@middleware
async def audit_middleware(request, call_next):
    response = await call_next(request)
    response.headers["X-Audited"] = "true"
    return response

@api.get("/reports")
@audit_middleware
async def reports():
    return {"ok": True}
```

Class-based middleware:

```python
from django_bolt import BoltAPI, Router
from django_bolt.middleware import Middleware

class RequestIdMiddleware(Middleware):
    async def process_request(self, request):
        response = await self.get_response(request)
        response.headers["X-Request-ID"] = "demo"
        return response

# Global (all routes)
api = BoltAPI(middleware=[RequestIdMiddleware])

# Router-level (routes in this router)
admin_router = Router(prefix="/admin", middleware=[RequestIdMiddleware])
api.include_router(admin_router)
```

#### Trailing slash modes

The `trailing_slash` parameter controls how route paths are normalized at registration time:

| Mode | Description | Example |
|------|-------------|---------|
| `"strip"` | Remove trailing slashes (default) | `/users/` → `/users` |
| `"append"` | Add trailing slashes | `/users` → `/users/` |
| `"keep"` | No normalization, keep paths as-is | `/users/` → `/users/` |

**Runtime behavior:** When a request doesn't match a route, Django-Bolt checks if the alternate path (with/without trailing slash) exists. If found, returns a **308 Permanent Redirect** to the canonical URL.

```python
# Default: strip trailing slashes
api = BoltAPI()

@api.get("/users/")  # Registered as /users
async def list_users():
    return []
# GET /users  → 200 OK
# GET /users/ → 308 Redirect to /users

# Append trailing slashes (Django convention)
api = BoltAPI(trailing_slash="append")

@api.get("/users")  # Registered as /users/
async def list_users():
    return []
# GET /users/  → 200 OK
# GET /users   → 308 Redirect to /users/

# Keep paths as defined
api = BoltAPI(trailing_slash="keep")

@api.get("/users/")  # Registered as /users/
async def list_users():
    return []
```

**Multiple APIs:** When auto-discovering multiple `api.py` files, each API's routes keep their own trailing slash format. Different apps can use different conventions.

### Route decorators

#### @api.get(path, **options)

Register a GET endpoint.

```python
@api.get("/users")
async def list_users():
    return []
```

#### @api.post(path, **options)

Register a POST endpoint.

```python
@api.post("/users")
async def create_user():
    return {}
```

#### @api.put(path, **options)

Register a PUT endpoint.

#### @api.patch(path, **options)

Register a PATCH endpoint.

#### @api.delete(path, **options)

Register a DELETE endpoint.

#### @api.head(path, **options)

Register a HEAD endpoint.

#### @api.options(path, **options)

Register an OPTIONS endpoint.

### Route options

All route decorators accept these options:

| Option | Type | Description |
|--------|------|-------------|
| `status_code` | `int` | Default response status code |
| `response_model` | `type` or `dict` | Schema for response validation. Pass a dict mapping status codes to types for per-status-code schemas (e.g., `{200: Ok, 404: Err}`) |
| `summary` | `str` | Short description for OpenAPI |
| `description` | `str` | Detailed description for OpenAPI |
| `tags` | `list[str]` | OpenAPI tags for grouping |
| `auth` | `list` | Authentication backends |
| `guards` | `list` | Permission guards |
| `include_in_schema` | `bool` | Include in OpenAPI docs |

### Class-based view decorators

#### @api.view(path)

Register a class-based view (APIView).

```python
@api.view("/resource")
class ResourceView(APIView):
    async def get(self, request):
        return {}
```

#### @api.viewset(path)

Register a ViewSet with automatic CRUD routing.

```python
@api.viewset("/items")
class ItemViewSet(ViewSet):
    async def list(self, request):
        return []
```

### WebSocket decorator

#### @api.websocket(path)

Register a WebSocket endpoint.

```python
@api.websocket("/ws")
async def websocket_handler(websocket: WebSocket):
    await websocket.accept()
```

### API mounting

#### api.mount(prefix, other_api)

Mount another BoltAPI under a prefix.

```python
api.mount("/v1", v1_api)
```

#### api.mount_asgi(path, app)

Mount an HTTP ASGI application under a static prefix.

See [ASGI Mounts](../topics/asgi-mounts.md) for full behavior, examples, and constraints.

Mounted ASGI apps are outside Bolt middleware/auth/CORS flow and currently use a buffered response bridge (not true streaming passthrough).

#### api.mount_django(path, app=None, *, clear_root_path=False)

Convenience wrapper for mounting Django's ASGI app (or a custom ASGI app) under a prefix.

See [ASGI Mounts](../topics/asgi-mounts.md) for usage examples and dispatch/conflict rules.

`mount_django()` inherits the same mount boundary and buffered-bridge behavior as `mount_asgi()`.
Use `clear_root_path=True` when Django URL patterns already include the mount prefix.

## Dependency injection

### Depends

Mark a parameter as a dependency.

```python
from django_bolt import Depends

async def get_db():
    return Database()

@api.get("/data")
async def get_data(db=Depends(get_db)):
    return db.query()
```

Dependencies can:

- Return any value
- Be async or sync functions
- Have their own dependencies
- Access request parameters

### get_current_user

Built-in dependency for getting the authenticated user.

```python
from django_bolt.auth import get_current_user

@api.get("/me", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def me(user=Depends(get_current_user)):
    return {"username": user.username}
```

## Request object

The request dict contains:

| Key | Type | Description |
|-----|------|-------------|
| `method` | `str` | HTTP method |
| `path` | `str` | Request path |
| `query` | `dict` | Query parameters |
| `params` | `dict` | Path parameters |
| `headers` | `dict` | Request headers |
| `body` | `bytes` | Raw request body |
| `context` | `dict` | Authentication context |

### Type-safe Request

```python
from django_bolt import Request

@api.get("/info")
async def info(request: Request):
    user = request.user        # Lazy-loaded Django user
    context = request.context  # Auth context dict
```

## Exceptions

### HTTPException

Base class for HTTP errors.

```python
from django_bolt.exceptions import HTTPException

raise HTTPException(status_code=404, detail="Not found")
```

### Built-in exceptions

| Exception | Status Code |
|-----------|-------------|
| `BadRequest` | 400 |
| `Unauthorized` | 401 |
| `Forbidden` | 403 |
| `NotFound` | 404 |
| `MethodNotAllowed` | 405 |
| `Conflict` | 409 |
| `UnprocessableEntity` | 422 |
| `InternalServerError` | 500 |

```python
from django_bolt.exceptions import NotFound, BadRequest

raise NotFound(detail="User not found")
raise BadRequest(detail="Invalid input")
```

### RequestValidationError

For validation errors with field-level details.

```python
from django_bolt.exceptions import RequestValidationError

errors = [
    {"loc": ["body", "email"], "msg": "Invalid email", "type": "value_error"}
]
raise RequestValidationError(errors)
```

## Parameter markers

### Header

Extract a header value.

```python
from django_bolt.param_functions import Header

async def handler(auth: Annotated[str, Header(alias="Authorization")]):
    pass
```

### Cookie

Extract a cookie value.

```python
from django_bolt.param_functions import Cookie

async def handler(session: Annotated[str, Cookie(alias="sessionid")]):
    pass
```

### Form

Extract form data.

```python
from django_bolt.param_functions import Form

async def handler(username: Annotated[str, Form()]):
    pass
```

### File

Extract uploaded files.

```python
from django_bolt.param_functions import File

async def handler(files: Annotated[list[dict], File(alias="file")]):
    pass
```
