---
icon: lucide/arrow-down-to-line
---

# Request Handling

This guide covers how Django-Bolt processes requests and how to access request data in your handlers.

## The request object

Access the full request using a `request` parameter:

```python
@api.get("/info")
async def request_info(request):
    return {
        "method": request.get("method"),
        "path": request.get("path"),
    }
```

## Request properties

The request dict contains:

| Property | Type | Description |
|----------|------|-------------|
| `method` | `str` | HTTP method (GET, POST, etc.) |
| `path` | `str` | Request path |
| `query` | `dict` | Query parameters |
| `params` | `dict` | Path parameters |
| `headers` | `dict` | Request headers |
| `body` | `bytes` | Raw request body |
| `context` | `dict` | Authentication context |

## Type-safe request

For better IDE support, use the `Request` type:

```python
from django_bolt import Request

@api.get("/profile", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def profile(request: Request):
    # IDE knows about .user, .context, .get(), etc.
    return {"user_id": request.user.id}
```

The `Request` type provides:

- `request.user` - Authenticated user (lazy loaded)
- `request.session` - Django session (requires SessionMiddleware)
- `await request.auser()` - Async user getter
- `request.context` - Authentication context dict
- `request.get(key, default)` - Get request property
- `request[key]` - Access request property

## Path parameters

Extract path parameters using curly braces in the route and matching function arguments:

```python
@api.get("/users/{user_id}/posts/{post_id}")
async def get_post(user_id: int, post_id: int):
    return {"user_id": user_id, "post_id": post_id}
```

Type conversion happens automatically:

- `int` - Converts to integer
- `float` - Converts to float
- `str` - Keeps as string (default)

Invalid conversions return a 422 Unprocessable Entity.

## Query parameters

Parameters without path placeholders become query parameters:

```python
@api.get("/search")
async def search(
    q: str,              # Required
    page: int = 1,       # Optional with default
    limit: int = 20,     # Optional with default
    sort: str | None = None  # Optional, None if not provided
):
    return {"q": q, "page": page, "limit": limit, "sort": sort}
```

If no type annotation is provided, the parameter is treated as a string.

## Request body

### JSON body

Use `msgspec.Struct` for validated JSON bodies:

```python
import msgspec

class CreateUser(msgspec.Struct):
    username: str
    email: str
    age: int | None = None

@api.post("/users")
async def create_user(user: CreateUser):
    return {"username": user.username}
```

### Raw body access

Access the raw body bytes:

```python
@api.post("/raw")
async def raw_body(request):
    body = request.get("body", b"")
    return {"size": len(body)}
```

## Headers

### Using Annotated

Extract specific headers:

```python
from typing import Annotated
from django_bolt.param_functions import Header

@api.get("/auth")
async def check_auth(
    authorization: Annotated[str, Header(alias="Authorization")]
):
    return {"auth": authorization}
```

### Optional headers

```python
@api.get("/optional-header")
async def optional_header(
    custom: Annotated[str | None, Header(alias="X-Custom")] = None
):
    return {"custom": custom}
```

### All headers

Access all headers from the request:

```python
@api.get("/headers")
async def all_headers(request):
    headers = request.get("headers", {})
    return {"headers": dict(headers)}
```

## Cookies

Extract cookie values:

```python
from typing import Annotated
from django_bolt.param_functions import Cookie

@api.get("/session")
async def get_session(
    session_id: Annotated[str, Cookie(alias="sessionid")]
):
    return {"session_id": session_id}
```

## Sessions

Access Django sessions when using `django_middleware=True` or `DjangoMiddleware(SessionMiddleware)`.

### Reading and writing session data

Use Django's async session methods in async handlers:

```python
@api.post("/counter")
async def increment_counter(request: Request):
    session = request.session

    # Async read with default
    count = await session.aget("count", 0)

    # Async write
    await session.aset("count", count + 1)

    return {"count": count + 1}
```

### Available async methods

| Method | Description |
|--------|-------------|
| `await session.aget(key, default)` | Get a session value |
| `await session.aset(key, value)` | Set a session value |
| `await session.apop(key, default)` | Remove and return a value |
| `await session.akeys()` | Get all session keys |
| `await session.aitems()` | Get all key-value pairs |
| `await session.aflush()` | Delete session and create new |
| `await session.acycle_key()` | Regenerate key, keep data |

### Sync properties (no DB access)

These are safe to use in async handlers as they don't access the database:

```python
session_key = request.session.session_key  # Current session key
request.session.clear()  # Clear cached data
```

!!! warning "Use async methods in async handlers"
    Using sync methods like `session["key"]` or `session.get()` in async handlers will raise `SynchronousOnlyOperation`. Always use `aget`, `aset`, etc.

### Complete example

```python
from django_bolt import BoltAPI, Request
from django.contrib.auth import alogin, alogout

api = BoltAPI(django_middleware=True)

@api.post("/login")
async def login(request: Request, username: str, password: str):
    user = await User.objects.filter(username=username).afirst()
    if user and user.check_password(password):
        await alogin(request, user)
        await request.session.aset("login_time", str(datetime.now()))
        return {"status": "ok"}
    return {"status": "error"}

@api.get("/profile")
async def profile(request: Request):
    user = await request.auser()
    if not user.is_authenticated:
        return {"error": "not logged in"}

    return {
        "username": user.username,
        "login_time": await request.session.aget("login_time"),
    }

@api.post("/logout")
async def logout(request: Request):
    await alogout(request)
    return {"status": "logged out"}
```

## Form data

### URL-encoded forms

```python
from typing import Annotated
from django_bolt.param_functions import Form

@api.post("/login")
async def login(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()]
):
    return {"username": username}
```

### Multipart forms

```python
@api.post("/profile")
async def update_profile(
    name: Annotated[str, Form()],
    bio: Annotated[str, Form()] = ""
):
    return {"name": name, "bio": bio}
```

## Parameter models

You can use `msgspec.Struct` or `Serializer` to group related parameters into a single validated object. This works with `Form()`, `Query()`, `Header()`, and `Cookie()`.

### Form models

Group form fields into a struct:

```python
import msgspec
from typing import Annotated
from django_bolt.param_functions import Form

class LoginForm(msgspec.Struct):
    username: str
    password: str
    remember_me: bool = False

@api.post("/login")
async def login(form: Annotated[LoginForm, Form()]):
    return {"username": form.username, "remember": form.remember_me}
```

With `Serializer` for custom validation:

```python
from django_bolt.serializers import Serializer, field_validator

class RegisterForm(Serializer):
    username: str
    email: str
    password: str

    @field_validator("username")
    def validate_username(cls, value):
        if len(value) < 3:
            raise ValueError("Username must be at least 3 characters")
        return value

@api.post("/register")
async def register(form: Annotated[RegisterForm, Form()]):
    return {"username": form.username}
```

### Query models

Group query parameters:

```python
class FilterParams(msgspec.Struct):
    limit: int = 10
    offset: int = 0
    search: str | None = None
    sort_by: str = "created_at"

@api.get("/items")
async def list_items(params: Annotated[FilterParams, Query()]):
    return {
        "limit": params.limit,
        "offset": params.offset,
        "search": params.search
    }
```

Request: `GET /items?limit=20&search=test`

### Header models

Group headers into a struct. Field names are converted from `snake_case` to `kebab-case` for HTTP header lookup:

```python
class AuthHeaders(msgspec.Struct):
    x_api_key: str           # maps to X-Api-Key header
    x_request_id: str | None = None  # maps to X-Request-Id header

@api.get("/secure")
async def secure_endpoint(headers: Annotated[AuthHeaders, Header()]):
    return {"api_key": headers.x_api_key}
```

Request: `GET /secure` with headers `X-Api-Key: secret123`

### Cookie models

Group cookies:

```python
class SessionCookies(msgspec.Struct):
    session_id: str
    theme: str = "light"
    language: str = "en"

@api.get("/preferences")
async def get_preferences(cookies: Annotated[SessionCookies, Cookie()]):
    return {"theme": cookies.theme, "language": cookies.language}
```

### Benefits of parameter models

| Feature | Individual params | Parameter models |
|---------|-------------------|------------------|
| Reusability | Copy-paste params | Define once, use everywhere |
| Validation | Per-field only | Custom `@field_validator` |
| Defaults | Per-field | Centralized in struct |
| IDE support | Basic | Full autocomplete |
| Documentation | Manual | Auto-generated from struct |

## File uploads

### File size limits: global vs per-view

There are two levels of file size validation in Django-Bolt:

- **Global limit**: Set by `BOLT_MAX_UPLOAD_SIZE` in your Django settings. This is the maximum file size the server will accept for any upload. If a file exceeds this, the request is rejected with a 413 error before your view runs.
- **Per-view limit**: Set by the `max_size` argument to the `File()` parameter in your view. This allows you to set a stricter limit for a specific endpoint, but it can never exceed the global limit.

**Example:**

```python
# settings.py
BOLT_MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB

# In your view
from django_bolt import FileSize, UploadFile
from django_bolt.param_functions import File
from typing import Annotated

@api.post("/upload")
async def upload_file(
    file: Annotated[
        UploadFile | None,
        File(max_size=FileSize.MB_10),  # 10 MB per-view limit
    ] = None,
):
    return {"filename": file.filename, "size": file.size}
```

In this example, files over 10 MB are rejected by the view, and files over 50 MB are rejected by the server. If you set `File(max_size=...)` higher than `BOLT_MAX_UPLOAD_SIZE`, the global limit always wins.

!!! warning
    The global limit (`BOLT_MAX_UPLOAD_SIZE`) is enforced **before** your view handler runs. You cannot override it in your view. Always set it to the maximum file size your server should ever accept.


Django-Bolt provides the `UploadFile` class for handling file uploads with Django integration.

### Basic file upload

```python
from typing import Annotated
from django_bolt import UploadFile
from django_bolt.params import File

@api.post("/upload")
async def upload(file: Annotated[UploadFile, File()]):
    content = await file.read()
    return {
        "filename": file.filename,
        "size": file.size,
        "content_type": file.content_type,
    }
```

### UploadFile properties

| Property | Type | Description |
|----------|------|-------------|
| `filename` | `str` | Original filename |
| `content_type` | `str` | MIME type |
| `size` | `int` | Size in bytes |
| `file` | `Django File` | Django File object for FileField |
| `headers` | `dict` | Multipart headers |

### UploadFile methods

| Method | Description |
|--------|-------------|
| `await read(size=-1)` | Read file content (async) |
| `await seek(offset)` | Seek to position (async) |
| `await close()` | Close the file (async) |
| `file.read()` | Read file content (sync) |

### File validation

Use `File()` parameters to validate uploads:

```python
from django_bolt import FileSize

@api.post("/upload")
async def upload(
    file: Annotated[UploadFile, File(
        max_size=FileSize.MB_10,           # Maximum 10MB
        min_size=1024,                      # Minimum 1KB
        allowed_types=["image/*", "application/pdf"],  # MIME types (wildcards supported)
    )]
):
    return {"filename": file.filename}
```

### FileSize enum

Use the `FileSize` enum for readable size limits:

```python
from django_bolt import FileSize

File(max_size=FileSize.MB_1)   # 1 MB
File(max_size=FileSize.MB_5)   # 5 MB
File(max_size=FileSize.MB_10)  # 10 MB
File(max_size=FileSize.MB_50)  # 50 MB
File(max_size=FileSize.MB_100) # 100 MB
```

### Multiple file uploads

```python
@api.post("/upload-multiple")
async def upload_multiple(
    files: Annotated[list[UploadFile], File(
        max_files=5,              # Maximum 5 files
        max_size=FileSize.MB_5,   # 5MB per file
    )]
):
    return {
        "count": len(files),
        "filenames": [f.filename for f in files],
    }
```

### Saving to Django FileField / ImageField

When a multipart request arrives, Django-Bolt parses it in Rust for performance — extracting file content, filename, content type, and size. The result is wrapped in an `UploadFile` object on the Python side, which gives you async read/write methods and automatic cleanup.

However, Django's `FileField` and `ImageField` don't know about `UploadFile` — they expect a `django.core.files.base.File` object. The `.file` property bridges this gap: it wraps the Rust-parsed file data in a Django `File` without an additional memory copy, so you can assign it directly to any model field:

```python
from myapp.models import Document, UserProfile

# Async handler with FileField
@api.post("/documents")
async def create_document(
    title: Annotated[str, Form()],
    upload: Annotated[UploadFile, File(max_size=FileSize.MB_10)],
):
    doc = Document(title=title)
    doc.file = upload.file  # assign Django File to FileField
    await doc.asave()
    return {"id": doc.id, "url": doc.file.url}

# ImageField works the same way
@api.post("/avatar")
async def upload_avatar(
    avatar: Annotated[UploadFile, File(
        max_size=FileSize.MB_5,
        allowed_types=["image/*"],
    )],
    request,
):
    profile = await UserProfile.objects.aget(user=request.user)
    profile.avatar = avatar.file  # assign Django File to ImageField
    await profile.asave()
    return {"avatar_url": profile.avatar.url}

# Sync handlers work too
@api.post("/sync-upload")
def sync_upload(upload: Annotated[UploadFile, File()]):
    doc = Document(title="Uploaded doc")
    doc.file = upload.file
    doc.save()
    return {"id": doc.id}
```

Since `.file` returns a standard Django `File`, it works transparently with any storage backend you have configured — `FileSystemStorage`, S3 via `django-storages`, Google Cloud Storage, etc.

### Mixed form and files

```python
@api.post("/submit")
async def submit(
    title: Annotated[str, Form()],
    description: Annotated[str, Form()],
    attachment: Annotated[UploadFile, File()] = None,
):
    return {
        "title": title,
        "has_attachment": attachment is not None,
    }
```

### Form struct with files

Combine form fields and file uploads in a struct:

```python
import msgspec

class ProfileForm(msgspec.Struct):
    name: str
    bio: str = ""
    avatar: UploadFile

@api.post("/profile")
async def update_profile(data: Annotated[ProfileForm, Form()]):
    return {
        "name": data.name,
        "avatar_filename": data.avatar.filename,
    }
```

### Validation errors

File validation returns structured errors:

```json
{
    "detail": [
        {
            "type": "file_too_large",
            "loc": ["body", "avatar"],
            "msg": "File exceeds maximum size of 10000000 bytes",
            "ctx": {"max_size": 10000000, "actual_size": 15000000}
        }
    ]
}
```

Error types:

| Type | Description |
|------|-------------|
| `file_missing` | Required file not uploaded |
| `file_too_large` | Exceeds `max_size` |
| `file_too_small` | Below `min_size` |
| `file_too_many` | Exceeds `max_files` |
| `file_invalid_content_type` | Not in `allowed_types` |

### Disk spooling

During multipart parsing, Rust keeps small files (<= `BOLT_MEMORY_SPOOL_THRESHOLD`, default 1 MB) in memory and writes larger files to a temporary file on disk to avoid memory spikes. `UploadFile` handles this transparently — disk-spooled files are lazily opened only when accessed. Temp files are cleaned up automatically after the request.

### Global upload settings

Configure file upload limits in settings:

```python
# settings.py
from django_bolt import FileSize

# Maximum upload size (requests exceeding this are rejected)
BOLT_MAX_UPLOAD_SIZE = FileSize.MB_10  # 10 MB global limit

# Memory threshold before spooling to disk (default: 1 MB)
# Files smaller than this are kept in memory; larger files are written to temp files
BOLT_MEMORY_SPOOL_THRESHOLD = 5 * 1024 * 1024  # 5 MB
```

See [Settings Reference](../ref/settings.md#file-upload-settings) for more details.

## Dependency injection

Use `Depends` for reusable parameter extractors:

```python
from django_bolt import Depends

async def get_pagination(page: int = 1, limit: int = 20):
    return {"page": page, "limit": limit, "offset": (page - 1) * limit}

@api.get("/items")
async def list_items(pagination=Depends(get_pagination)):
    return {"pagination": pagination}
```

Dependencies can be chained and cached. See [Dependency Injection](../ref/api.md#dependency-injection) for more details.

## Validation errors

When request validation fails, Django-Bolt returns a 422 Unprocessable Entity with details:

```json
{
    "detail": [
        {
            "loc": ["body", "email"],
            "msg": "Expected `str` - at `$.email`",
            "type": "validation_error"
        }
    ]
}
```

Common validation scenarios that return 422:

| Scenario | Example Message |
|----------|-----------------|
| Missing required query param | `Missing required query parameter: page` |
| Missing required header | `Missing required header: x-api-key` |
| Missing required cookie | `Missing required cookie: session` |
| Missing required form field | `Missing required form field: username` |
| Missing required file | `Missing required file: document` |
| Type conversion failure | `Invalid integer value: 'abc'` |
| Struct field validation | `name: Name must be at least 3 characters` |
