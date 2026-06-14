---
icon: lucide/route
---

# Routing

This guide explains how routing works in Django-Bolt and covers all the ways you can define API endpoints.

## Basic routing

Routes are defined using decorator methods on a `BoltAPI` instance:

```python
from django_bolt import BoltAPI

api = BoltAPI()

@api.get("/users")
async def list_users():
    return {"users": []}

@api.post("/users")
async def create_user():
    return {"created": True}
```

## HTTP methods

Django-Bolt supports all common HTTP methods:

| Decorator | HTTP Method | Typical use |
|-----------|-------------|-------------|
| `@api.get()` | GET | Retrieve resources |
| `@api.post()` | POST | Create resources |
| `@api.put()` | PUT | Replace resources |
| `@api.patch()` | PATCH | Partial updates |
| `@api.delete()` | DELETE | Remove resources |
| `@api.head()` | HEAD | Get headers only |
| `@api.options()` | OPTIONS | Get allowed methods |

Example with all methods:

```python
@api.get("/items")
async def list_items():
    return {"items": []}

@api.post("/items")
async def create_item():
    return {"created": True}

@api.put("/items/{item_id}")
async def replace_item(item_id: int):
    return {"replaced": True}

@api.patch("/items/{item_id}")
async def update_item(item_id: int):
    return {"updated": True}

@api.delete("/items/{item_id}")
async def delete_item(item_id: int):
    return {"deleted": True}

@api.head("/items")
async def head_items():
    return {}  # Body not sent for HEAD

@api.options("/items")
async def options_items():
    from django_bolt import Response
    return Response({}, headers={"Allow": "GET, POST, PUT, PATCH, DELETE"})
```

## Path parameters

Capture dynamic segments of the URL using curly braces:

```python
@api.get("/users/{user_id}")
async def get_user(user_id: int):
    return {"user_id": user_id}

@api.get("/posts/{post_id}/comments/{comment_id}")
async def get_comment(post_id: int, comment_id: int):
    return {"post_id": post_id, "comment_id": comment_id}
```

See [Request Handling](requests.md) for details on path parameters, query parameters, headers, cookies, form data, and file uploads.

## Route options

The route decorator accepts additional options:

```python
@api.get(
    "/users/{user_id}",
    status_code=200,           # Default response status code
    summary="Get user",        # Short description for OpenAPI
    description="Get a user by ID",  # Detailed description
    tags=["users"],            # OpenAPI tags for grouping
    response_model=UserSchema, # Response validation schema
)
async def get_user(user_id: int):
    """This docstring also appears in OpenAPI docs."""
    return {"user_id": user_id}
```

## URL names and reversing

Bolt routes live in Rust's router, not Django's URLconf, so Django can't see them by default. Wire them up once and Django's native `reverse()`, `reverse_lazy()`, and the `{% url %}` template tag resolve Bolt route names like any other view.

### Wiring it up

Include `django_bolt.urls` in your project's `ROOT_URLCONF`:

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    path("", include("django_bolt.urls")),
    # ... your other patterns
]
```

This contributes a **reverse-only** entry for every named Bolt route. The registered views never run (Bolt still serves these paths in Rust) — they exist purely so Django can reverse the names.

### Naming a route

Pass `name=` to any route decorator:

```python
@api.get("/missions/{mission_id}", name="mission-detail")
async def get_mission(mission_id: int):
    return {"id": mission_id}
```

```python
from django.urls import reverse

reverse("mission-detail", kwargs={"mission_id": 42})  # "/missions/42"
```

```html
{% url "mission-detail" mission_id=42 %}
```

Path converters and catch-alls come from Django's own resolver, so `args`/`kwargs`, `query`, and `fragment` all work. A Bolt `{name:path}` catch-all reverses through Django's `<path:name>` converter (accepts slashes); other `{name:type}` hints are untyped on reverse, matching the router.

### Derived names

If you omit `name=`, the name is the **verbatim Python identifier** — the function name for routes, the class name for class-based views — with no transformation:

```python
@api.get("/y")
async def get_mission():   # name == "get_mission"
    return {}
```

Reversing against a derived name that no longer exists raises Django's usual `NoReverseMatch`, so renames surface immediately. Name a route explicitly whenever you intend to reverse it.

### Namespaces

Namespaces are **opt-in**, like Django's `app_name`. Pass `namespace=` to a `BoltAPI` and its routes reverse as `namespace:name`:

```python
api = BoltAPI(namespace="missions")

@api.get("/missions/{mission_id}", name="detail")
async def get_mission(mission_id: int):
    return {}

reverse("missions:detail", kwargs={"mission_id": 42})  # "/missions/42"
```

A namespaced route reverses **only** under its namespace; the bare name won't resolve.

### Class-based views and viewsets

`view()`, `viewset()`, and `@action` all accept `name=`. A viewset names each route `{base}-{action}`:

```python
@api.viewset("/users", name="user")
class UserViewSet(ViewSet):
    async def list(self, request): ...           # name == "user-list"
    async def partial_update(self, request): ...  # name == "user-partial_update"

    @action(["GET"], detail=False)
    async def recent(self, request): ...          # name == "user-recent"
```

Without `name=`, the base falls back to the verbatim class name (`UserViewSet-list`), so set `name=` on viewsets you intend to reverse.

### Collisions

Names are resolved when the urlpatterns are built:

- Several methods on one path share a name and are deduped (no error).
- An explicit `name=` wins over a derived name with the same key.
- Two **explicit** names mapping to different paths raise `ImproperlyConfigured`.

## Sync handlers

While async handlers are recommended, you can also use synchronous functions:

```python
@api.get("/sync")
def sync_handler():
    return {"sync": True}
```

Sync handlers are automatically wrapped to run in a thread pool.

## WebSocket routes

Define WebSocket endpoints using `@api.websocket()`:

```python
from django_bolt import WebSocket

@api.websocket("/ws/echo")
async def echo(websocket: WebSocket):
    await websocket.accept()
    async for message in websocket.iter_text():
        await websocket.send_text(f"Echo: {message}")
```

See the [WebSocket guide](websocket.md) for more details.

## Auto-discovery

Django-Bolt automatically discovers `api.py` files in:

1. Your project directory (where `settings.py` is)
2. Each installed Django app

All routes from discovered files are combined into a single router. This lets you organize routes per app:

```
myproject/
    myproject/
        settings.py
        api.py              # /health, /docs
    users/
        api.py              # /users, /users/{id}
    products/
        api.py              # /products, /products/{id}
```

## Multiple API instances

You can create multiple `BoltAPI` instances and mount them:

```python
# users/api.py
from django_bolt import BoltAPI

api = BoltAPI()

@api.get("/users")
async def list_users():
    return {"users": []}
```

```python
# myproject/api.py
from django_bolt import BoltAPI
from users.api import api as users_api

api = BoltAPI()

# Mount users API under /api/v1
api.mount("/api/v1", users_api)
```

Routes from `users_api` are now available at `/api/v1/users`.

## ASGI mounts

If you want to mount Django or other ASGI apps under path prefixes, see [ASGI Mounts](asgi-mounts.md).

## Trailing slash handling

Django-Bolt normalizes trailing slashes at route registration time and uses **Starlette-style redirects** at runtime. By default, trailing slashes are stripped from paths:

```python
api = BoltAPI()

@api.get("/users/")  # Registered as /users
async def list_users():
    return []
```

### Trailing slash modes

You can control this behavior with the `trailing_slash` parameter:

| Mode | Description | Example |
|------|-------------|---------|
| `"strip"` | Remove trailing slashes (default) | `/users/` → `/users` |
| `"append"` | Add trailing slashes | `/users` → `/users/` |
| `"keep"` | No normalization | `/users/` → `/users/` |

### Runtime redirect behavior

When a request URL doesn't exactly match a registered route, Django-Bolt checks if the alternate path (with or without trailing slash) exists. If it does, a **308 Permanent Redirect** is returned to the canonical URL:

```python
api = BoltAPI(trailing_slash="append")

@api.get("/users")  # Registered as /users/
async def list_users():
    return []

# GET /users   → 308 Redirect to /users/
# GET /users/  → 200 OK (canonical URL)
```

This Starlette-style approach means:
- **Both URLs work** - users can access either, but one redirects
- **SEO-friendly** - search engines see the redirect and index the canonical URL
- **Minimal overhead** - redirect check only happens when route doesn't match

### Strip mode (default)

The default mode removes trailing slashes, which produces clean URLs:

```python
api = BoltAPI()  # trailing_slash="strip" is the default

@api.get("/users/")   # Registered as /users
@api.get("/items")    # Registered as /items

# GET /users/  → 308 Redirect to /users
# GET /users   → 200 OK
```

### Append mode

Use append mode to follow Django's URL convention where paths end with slashes:

```python
api = BoltAPI(trailing_slash="append")

@api.get("/users")    # Registered as /users/
@api.get("/items/")   # Registered as /items/

# GET /users   → 308 Redirect to /users/
# GET /users/  → 200 OK
```

### Keep mode

Use keep mode when you need explicit control over each path:

```python
api = BoltAPI(trailing_slash="keep")

@api.get("/users")    # Registered as /users
@api.get("/items/")   # Registered as /items/

# GET /users/  → 308 Redirect to /users (if /users exists)
# GET /items   → 308 Redirect to /items/ (if /items/ exists)
```

### Multiple APIs with different settings

When Django-Bolt auto-discovers and merges multiple `api.py` files, **each API's routes keep their own trailing slash format**. This allows different apps to use different conventions:

```python
# myproject/api.py
api = BoltAPI(trailing_slash="append")

@api.get("/items")  # Registered as /items/
async def list_items():
    return []

# users/api.py
api = BoltAPI()  # Default: trailing_slash="strip"

@api.get("/users")  # Registered as /users
async def list_users():
    return []
```

After merging:
- `GET /items` → 308 Redirect to `/items/`
- `GET /items/` → 200 OK
- `GET /users/` → 308 Redirect to `/users`
- `GET /users` → 200 OK

Each route respects its original API's `trailing_slash` setting because routes are normalized at registration time, before the merge happens.

### Mounted APIs

When mounting APIs with `api.mount()`, the **parent's** `trailing_slash` setting is applied to all child routes:

```python
parent = BoltAPI(trailing_slash="append")
child = BoltAPI(trailing_slash="strip")  # Child's setting is IGNORED when mounted

@child.get("/users")  # Child normalizes to /users
async def list_users():
    return []

parent.mount("/api", child)
# Route is /api/users/ (parent's "append" mode is applied)
```

This differs from auto-discovery, where each API keeps its own setting.

## URL prefix

Apply a prefix to all routes using the `prefix` parameter:

```python
api = BoltAPI(prefix="/api/v1")

@api.get("/users")  # Accessible at /api/v1/users
async def list_users():
    return []
```

This is useful for versioning your API or grouping routes under a common path.
