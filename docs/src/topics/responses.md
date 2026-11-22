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

A scalar is also a JSON response. Return an `int`, a `float`, a `bool`, a `Decimal`, a `UUID`, an `Enum` member, or a `date`, `time`, `datetime` or `timedelta`:

```python
@api.get("/count")
async def count() -> int:
    return 3  # Body: 3, Content-Type: application/json
```

`None` is the JSON `null` when the response type allows it, such as `-> None` or `-> int | None`. With no such response type, a `None` result is an error, because the handler has no `return`. A `-> None` route validates its result: a result that is not `None` is a response validation error. A response object, such as `JSON(...)` or `Response(...)`, has its own body and is not validated on a `-> None` route. A `str` and `bytes` are not JSON, see [Returning strings and bytes](#returning-strings-and-bytes).

### Values in a JSON response

A dict, a list or a `msgspec.Struct` can contain these values. The table gives the JSON form of each value.

| Value | JSON |
| --- | --- |
| `str`, `int`, `float`, `bool`, `None` | the same JSON value |
| `Decimal`, `Fraction` | a string, such as `"1.50"` or `"1/3"` |
| `UUID`, `Path`, an IP address or network, a compiled `re.Pattern`, `UserString` | a string |
| `date`, `time`, `datetime`, `timedelta` | an ISO 8601 string |
| `Enum` member | its value |
| `bytes`, `bytearray` | a base64 string |
| `dict`, `ChainMap`, `MappingProxyType`, `UserDict` | an object |
| `list`, `tuple`, `set`, `frozenset`, `deque`, `UserList`, `range`, `array.array`, a dict view (`d.keys()`, `d.values()`, `d.items()`) | an array |
| a generator, a `map` object, a `zip` object | an array of the items that it gives |

A subclass of a type in the table has the JSON form of its base type. The items of a container go through the same rules. A typed response validates first. Then a value must match the declared type, so `-> list[int]` refuses a `range`.

Bolt refuses a value with no clear JSON form, such as a `ZoneInfo`, a `datetime.timezone`, a `SimpleNamespace` or a `complex`. Django's `DjangoJSONEncoder` also refuses these values. The response is then a 500 with `TypeError: Unsupported type`. Convert such a value before you return it, for example `str(zone)` or `vars(namespace)`.

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

The `auth` context processor gives the template the `user` of the request. In an async handler, a template that reads `{{ user }}` loads the user, and the event loop waits for the query. To not block the loop, load the user first:

```python
@api.get("/account")
async def account(request: Request):
    await request.auser()  # the template then reads the loaded user
    return render(request, "myapp/account.html")
```

### Template responses

`render()` renders the template at once. To render it later, return a `TemplateResponse`. It is the `TemplateResponse` class of Django, with the same arguments:

```python
from django_bolt import Request
from django_bolt.responses import TemplateResponse

@api.get("/page")
async def show_page(request: Request):
    return TemplateResponse(request, "myapp/page.html", {"title": "My Page"})
```

Bolt renders the template after the handler returns. A route behind Django middleware first calls the `process_template_response` hook of each middleware. A hook can change `template_name` and `context_data` before the render. See [Django middleware integration](middleware.md#django-middleware-integration). A route with no Django middleware renders it with no hooks.

Pass `status`, `content_type` and `headers` as in Django.

The render can read a QuerySet in the context, also in an async handler. Bolt runs the hooks and the render where a sync ORM call can run.

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

A `FileResponse` for a path outside these directories raises `ResponseFileNotAllowed`, and Bolt answers 403, also when the path names no file. When the lookup fails, Bolt follows the links of the path, as `realpath` does, to where the lookup stops. Then it completes the path from there as text. A `FileResponse` for a path that names no regular file raises `ResponseFileNotFound`, and Bolt answers 404. Examples are a missing file, a directory and a path that is too long. The path often comes from the request, so a client does not get a 500 for it. The errors subclass `PermissionError` and `FileNotFoundError`. The 404 and 403 responses do not show the path. Bolt answers 500 for a `FileNotFoundError` or a `PermissionError` of other code, because it is a server fault.

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

### Errors in a stream

Bolt sends the status and the headers before the first chunk.
Thus an error in the generator cannot become a 500.
The body ends at the error, and the client gets the chunks before it.
Bolt logs the error with its traceback to the `django_bolt.streaming` logger, at level ERROR.

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

Each response copies its `headers` dict.
A change through `response.headers` does not change the dict that you gave.
Thus many responses can use one module-level dict.

```python
from django_bolt import JSON

COMMON_HEADERS = {"X-Service": "api"}

@api.get("/items")
async def list_items():
    response = JSON({"items": []}, headers=COMMON_HEADERS)
    response.headers["X-Count"] = "0"  # COMMON_HEADERS does not change
    return response
```

## Setting cookies

Use the `.set_cookie()` method on any response type:

```python
from django_bolt import Response, JSON

@api.post("/login")
async def login():
    return Response({"logged_in": True}).set_cookie("session", "abc123")
```

Bolt writes the value as Django `set_cookie` writes it.
A value with a space, a quote, `;` or another unsafe character gets double quotes and escapes.
`request.cookies` and `Cookie()` remove them again, so a handler reads the value that Bolt sets.
Bolt does not set a value that has a control character or a character above U+00FF, and logs a warning.
Django cannot send these values either.
To keep other text in a cookie, encode it first, for example with `urllib.parse.quote()`.

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

The letter case of `samesite` has no meaning, so `"lax"` gives `SameSite=Lax`. Another value raises `ValueError` when you set the cookie, as `HttpResponse.set_cookie()` of Django does.

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

A name that starts with `__Secure-` or `__Host-` gets the `Secure` flag, as in Django.
A browser ignores the delete of such a cookie without this flag.

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

The schema describes the body of the default status code of the route. Bolt validates a `JSON` or `Response` only when its status code is that default. A response with another status code, such as a 400 with a map of field errors, is encoded as-is. To document other status codes, use the per-status-code form below. It does not validate them either.

```python
@api.post("/users", status_code=201)
async def create_user(body: UserCreate) -> User:
    if await username_taken(body.username):
        return JSON({"username": ["Already taken."]}, status_code=400)  # not validated
    return JSON(await create(body), status_code=201)  # validated as User
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

The status of the tuple must be an int from 200 to 599. A bool is not a status: `return True, data` gives a 500.

The data of the tuple can be a response object. Bolt sends the response object with the status of the tuple. The body, the headers and the cookies come from the response object:

```python
return 404, JSON({"detail": "Not found"}, headers={"X-Reason": "missing"})  # sent as 404
return 404, PlainText("gone")                                               # text/plain, sent as 404
```

**JSON() return** — `JSON(data, status_code=...)` also selects the schema:

```python
return JSON({"detail": "Not found"}, status_code=404)
```

**Bare return** — A plain dict or list uses the default status code. That is the `status_code` of the decorator, or else the lowest 2xx code in the map. A map with no 2xx code, such as `{404: Error}`, keeps the default status 200:

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

A key is an integer status code or `...`. The dict needs one integer key or more. Each other key, for example `"404"`, `404.0` or `True`, stops registration with `TypeError`.

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

A response status must be an int from 200 to 599 ([RFC 9110, section 15](https://www.rfc-editor.org/rfc/rfc9110#section-15)). A 1xx status is interim and cannot be the status of a response. A `status_code=` of a route outside this range raises `ValueError` at the decorator. A status outside this range from a handler, for example `JSON(data, status_code=700)` or `HTTPException(status_code=100)`, gives a 500, and the log names the status.

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

A response type that names `str` or `bytes` allows such a result with no check, for example `-> str`, `-> str | None` or `-> bytes | Item`. Each other response type validates the result:

```python
@api.get("/count")
async def count() -> int:
    return "abc"  # 500: response validation error
```

A `str` that the response type converts is JSON. For example, `-> UUID` with a `str` result sends the UUID in quotes as `application/json`.

This applies to `-> dict` and `-> list` too. A `-> dict` route takes a `dict`. A `-> list` route takes a `list`, a `tuple` or a QuerySet. Each other result is a response validation error, for each kind of handler. Examples are a `str`, a number, a list for `-> dict`, a dict for `-> list`, and a `Struct` or a generator. A response object, such as `JSON(...)`, has no such check.
A `-> list[UserSerializer]` route validates that the result is a list. A result such as `3` or a `dict` is a response validation error, and `None` is the empty list.
A route with `validate_response=False` does no such check. It sends a `str` as `text/plain` and `bytes` as `application/octet-stream`.

## Empty responses

For 204 No Content responses:

```python
from django_bolt import Response

@api.delete("/items/{item_id}", status_code=204)
async def delete_item(item_id: int):
    # ... delete the item ...
    return Response(status_code=204)
```
