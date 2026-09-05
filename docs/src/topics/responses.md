---
icon: lucide/arrow-up-from-line
---

# Responses

This guide covers all the response types available in Django-Bolt and how to use them.

## JSON responses

Returning a dict, list, or `msgspec.Struct` automatically creates a JSON response:

```python
@api.get("/data")
async def get_data():
    return {"message": "Hello", "count": 42}

@api.get("/items")
async def get_items():
    return [{"id": 1}, {"id": 2}]
```

### Custom status codes and headers

Use the `JSON` class for more control:

```python
from django_bolt import JSON

@api.post("/users")
async def create_user():
    return JSON(
        {"id": 1, "username": "john"},
        status_code=201,
        headers={"X-Created-By": "django-bolt"}
    )
```

### Status code constants and helpers

You can use built-in status code constants for readability:

```python
from django_bolt import status

@api.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user():
    ...
```

Code classification helpers are also available:

```python
if status.is_client_error(response.status_code):
    ...

if status.is_server_error(response.status_code):
    ...
```

## Plain text

Return plain text responses:

```python
from django_bolt.responses import PlainText

@api.get("/hello")
async def hello():
    return PlainText("Hello, World!")

@api.get("/status")
async def status():
    return PlainText("OK", status_code=200, headers={"X-Status": "healthy"})
```

## HTML

Return HTML content:

```python
from django_bolt.responses import HTML

@api.get("/page")
async def page():
    return HTML("<h1>Welcome</h1><p>This is HTML content.</p>")

@api.get("/template")
async def template():
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>My Page</title></head>
    <body><h1>Hello</h1></body>
    </html>
    """
    return HTML(html)
```

### Django templates

Use the `render()` function to render Django templates. It works like [Django's `render()` shortcut](https://docs.djangoproject.com/en/dev/topics/http/shortcuts/#render):

```python
from django_bolt import Request
from django_bolt.shortcuts import render

@api.get("/page")
async def show_page(request: Request):
    return render(request, "myapp/page.html", {
        "title": "My Page",
        "items": ["item1", "item2"],
    })
```

Use standard Django templates - nothing special required:

```html
<!-- templates/myapp/page.html -->
<!DOCTYPE html>
<html>
<head><title>{{ title }}</title></head>
<body>
    <h1>{{ title }}</h1>
    <ul>
    {% for item in items %}
        <li>{{ item }}</li>
    {% endfor %}
    </ul>
</body>
</html>
```

Parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `request` | `Request` | required | The request object |
| `template_name` | `str` | required | Path to the template file |
| `context` | `dict` | `None` | Template context variables |
| `content_type` | `str` | `None` | Response content type |
| `status` | `int` | `200` | HTTP status code |
| `using` | `str` | `None` | Template engine to use |

## Redirects

Redirect to another URL:

```python
from django_bolt.responses import Redirect

@api.get("/old-page")
async def old_page():
    return Redirect("/new-page")

@api.get("/external")
async def external():
    return Redirect("https://example.com", status_code=302)
```

Redirect status codes:

- `301` - Permanent redirect
- `302` - Temporary redirect (Found)
- `303` - See Other
- `307` - Temporary redirect (default, preserves method)
- `308` - Permanent redirect (preserves method)

## File downloads

### In-memory files

Use `File` for small files that can be loaded into memory:

```python
from django_bolt.responses import File

@api.get("/download")
async def download():
    return File(
        "/path/to/file.pdf",
        filename="document.pdf",
        media_type="application/pdf"
    )
```

### Streaming files

Use `FileResponse` for larger files that should be streamed:

```python
from django_bolt.responses import FileResponse

@api.get("/video")
async def video():
    return FileResponse(
        "/path/to/video.mp4",
        filename="video.mp4",
        media_type="video/mp4"
    )
```

`FileResponse` streams the file directly without loading it entirely into memory.

### File security

Configure allowed directories in `settings.py`:

```python
BOLT_ALLOWED_FILE_PATHS = [
    "/var/app/uploads",
    "/var/app/public",
]
```

When configured, `FileResponse` only serves files within these directories, preventing path traversal attacks.

## Streaming responses

Stream data incrementally using generators:

```python
from django_bolt import StreamingResponse

@api.get("/stream")
async def stream():
    def generate():
        for i in range(100):
            yield f"chunk {i}\n"

    return StreamingResponse(generate(), media_type="text/plain")
```

### Async generators

For async operations, use async generators:

```python
import asyncio

@api.get("/async-stream")
async def async_stream():
    async def generate():
        for i in range(10):
            await asyncio.sleep(0.1)
            yield f"data: {i}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

### Server-Sent Events (SSE)

For SSE endpoints, see the dedicated [Server-Sent Events guide](sse.md). Django-Bolt provides `EventSourceResponse` with automatic SSE framing, compression skipping, and keep-alive pings:

```python
from collections.abc import AsyncIterable
from django_bolt.responses import EventSourceResponse

@api.get("/events", response_class=EventSourceResponse)
async def events() -> AsyncIterable[dict]:
    for i in range(10):
        yield {"count": i}
        await asyncio.sleep(1)
```

## Response with custom headers

Use the `Response` class for complete control:

```python
from django_bolt import Response

@api.options("/items")
async def options_items():
    return Response(
        {},
        status_code=204,
        headers={"Allow": "GET, POST, PUT, DELETE"}
    )
```

## Setting cookies

Use the `.set_cookie()` method on any response type:

```python
from django_bolt import Response, JSON

@api.post("/login")
async def login():
    return Response({"logged_in": True}).set_cookie("session", "abc123")
```

### Cookie options

Pass additional options to control cookie behavior:

```python
@api.post("/login")
async def login():
    return JSON({"message": "Logged in"}).set_cookie(
        name="session",
        value="abc123",
        max_age=3600,           # Expires in 3600 seconds
        path="/",               # Cookie path (default: "/")
        domain=".example.com",  # Cookie domain
        secure=True,            # Only send over HTTPS
        httponly=True,          # Not accessible via JavaScript
        samesite="Lax",         # CSRF protection: "Strict", "Lax", or "None"
    )
```

Cookie attributes:

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | required | Cookie name |
| `value` | `str` | `""` | Cookie value |
| `max_age` | `int` | `None` | Seconds until expiration |
| `expires` | `datetime \| str` | `None` | Expiration date |
| `path` | `str` | `"/"` | Cookie path |
| `domain` | `str` | `None` | Cookie domain |
| `secure` | `bool` | `False` | HTTPS only |
| `httponly` | `bool` | `False` | No JavaScript access |
| `samesite` | `str \| False` | `"Lax"` | `"Strict"`, `"Lax"`, `"None"`, or `False` to omit |

### Multiple cookies

Chain multiple `.set_cookie()` calls:

```python
@api.post("/login")
async def login():
    return Response({"logged_in": True}) \
        .set_cookie("session", "abc123", httponly=True) \
        .set_cookie("preferences", "dark-mode", max_age=86400 * 365)
```

### Deleting cookies

Use `.delete_cookie()` to remove a cookie:

```python
@api.post("/logout")
async def logout():
    return Response({"logged_out": True}).delete_cookie("session")
```

With path and domain (must match the original cookie):

```python
@api.post("/logout")
async def logout():
    return Response({"logged_out": True}).delete_cookie(
        "session",
        path="/app",
        domain=".example.com"
    )
```

### Set and delete in one response

```python
@api.post("/refresh")
async def refresh():
    return Response({"refreshed": True}) \
        .delete_cookie("old_session") \
        .set_cookie("new_session", "xyz789", httponly=True)
```

### Cookies on all response types

The `.set_cookie()` and `.delete_cookie()` methods work on all response types:

```python
from django_bolt import Response, JSON, StreamingResponse
from django_bolt.responses import HTML, PlainText, Redirect

# JSON
JSON({"ok": True}).set_cookie("api_token", "secret")

# HTML
HTML("<h1>Welcome</h1>").set_cookie("visited", "true")

# PlainText
PlainText("OK").set_cookie("status", "checked")

# Redirect with cookie (e.g., after login)
Redirect("/dashboard").set_cookie("just_logged_in", "1", max_age=5)

# Streaming response
StreamingResponse(generate()).set_cookie("stream_id", "12345")
```

### Using the Cookie class directly

For advanced use cases, create `Cookie` objects directly:

```python
from django_bolt import Cookie

cookie = Cookie(
    name="session",
    value="abc123",
    max_age=3600,
    secure=True,
    httponly=True,
    samesite="Strict",
)

# Get the Set-Cookie header value
header_value = cookie.to_header_value()
# "session=abc123; Max-Age=3600; Path=/; Secure; HttpOnly; SameSite=Strict"
```

## Response validation

Validate response data against a schema using `response_model`:

```python
import msgspec

class User(msgspec.Struct):
    id: int
    username: str
    email: str

@api.get("/users/{user_id}", response_model=User)
async def get_user(user_id: int):
    return {"id": user_id, "username": "john", "email": "john@example.com"}
```

If the response doesn't match the schema, a 500 error is returned.

You can also use return type annotations:

```python
@api.get("/users/{user_id}")
async def get_user(user_id: int) -> User:
    return {"id": user_id, "username": "john", "email": "john@example.com"}
```

### Per-status-code response schemas

`response_model` also accepts a dict mapping status codes to types. Each code gets its own OpenAPI response entry. Bolt encodes the body as-is and does not validate it.

```python
import msgspec

class Item(msgspec.Struct):
    id: int
    name: str

class Error(msgspec.Struct):
    detail: str

@api.get("/items/{item_id}", response_model={200: Item, 404: Error})
async def get_item(item_id: int):
    item = await find_item(item_id)
    if item is None:
        return 404, {"detail": "Item not found"}
    return 200, {"id": item.id, "name": item.name}
```

**Tuple return** — Return `(status_code, data)` to select the schema by code:

```python
return 200, {"id": 1, "name": "Alice"}   # documented as Item
return 404, {"detail": "Not found"}       # documented as Error
```

**JSON() return** — `JSON(data, status_code=...)` also selects the schema:

```python
return JSON({"detail": "Not found"}, status_code=404)
```

**Bare return** — A plain dict or list uses the default status code (lowest 2xx in the map):

```python
@api.get("/items", response_model={200: list[Item], 400: Error})
async def list_items():
    return [{"id": 1, "name": "Alice"}]  # documented as list[Item] at 200
```

**204 No Content** — Use `{204: None}` with `return (204, None)` for empty body responses:

```python
@api.delete("/items/{item_id}", response_model={204: None, 404: Error})
async def delete_item(item_id: int):
    deleted = await try_delete(item_id)
    if not deleted:
        return 404, {"detail": "Item not found"}
    return 204, None
```

**Ellipsis catch-all** — Use `...` as a key to document any unmapped status code:

```python
@api.get("/items/{item_id}", response_model={200: Item, ...: Error})
async def get_item(item_id: int):
    ...  # any non-200 status code is documented as Error
```

**Explicit status_code** — Override the auto-detected default status code:

```python
@api.post("/items", response_model={201: Item, 400: Error}, status_code=201)
async def create_item():
    return {"id": 1, "name": "New"}  # bare return uses 201 instead of auto-detected
```

**Typed error bodies** — Raise `HTTPException` with `body=` to send a typed body from anywhere. The handler return annotation stays the success type, so type checkers can check it:

```python
@api.post("/plans/{pk}/start", response_model={200: Plan, 400: Error})
async def start_plan(request, pk: int) -> Plan:
    if not healthy:
        raise HTTPException(400, body=Error(detail="not healthy"))
    return Plan(id=pk)
```

`body` replaces the default `{"detail": ...}` envelope. Bolt encodes it as-is and does not validate it. An unmapped status code is sent as-is too.

## Setting default status codes

Set a default status code for an endpoint:

```python
@api.post("/users", status_code=201)
async def create_user():
    return {"id": 1, "username": "john"}
```

OpenAPI documents this status code even when the handler has no response annotation.

## Returning strings and bytes

Returning a string creates a plain text response:

```python
@api.get("/text")
async def text():
    return "Hello"  # Content-Type: text/plain
```

Returning bytes creates an octet-stream response:

```python
@api.get("/bytes")
async def bytes_response():
    return b"binary data"  # Content-Type: application/octet-stream
```

## Empty responses

For 204 No Content responses:

```python
from django_bolt import Response

@api.delete("/items/{item_id}", status_code=204)
async def delete_item(item_id: int):
    # ... delete the item ...
    return Response(status_code=204)
```
