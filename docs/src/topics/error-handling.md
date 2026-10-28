---
icon: lucide/triangle-alert
---

# Error handling

Django-Bolt provides a structured exception hierarchy for HTTP errors and automatic error response formatting.

## Exception classes

### HTTPException

The base class for all HTTP errors:

```python
from django_bolt.exceptions import HTTPException

raise HTTPException(status_code=400, detail="Bad request")
```

Parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status_code` | `int` | required | HTTP status code |
| `detail` | `str` | HTTP phrase | Error message |
| `headers` | `dict` | `{}` | Response headers |
| `extra` | `dict` | `None` | Additional error data |

### Specialized exceptions

Django-Bolt provides pre-configured exceptions for common status codes:

```python
from django_bolt.exceptions import (
    # 4xx Client Errors
    BadRequest,           # 400
    Unauthorized,         # 401
    Forbidden,            # 403
    NotFound,             # 404
    MethodNotAllowed,     # 405
    NotAcceptable,        # 406
    Conflict,             # 409
    Gone,                 # 410
    UnprocessableEntity,  # 422
    TooManyRequests,      # 429

    # 5xx Server Errors
    InternalServerError,  # 500
    BadGateway,           # 502
    ServiceUnavailable,   # 503
    GatewayTimeout,       # 504
)
```

Usage:

```python
@api.get("/users/{user_id}")
async def get_user(user_id: int):
    user = await User.objects.filter(id=user_id).afirst()
    if not user:
        raise NotFound(detail="User not found")
    return {"id": user.id, "username": user.username}
```

### Custom headers

Add headers to error responses:

```python
raise Unauthorized(
    detail="Authentication required",
    headers={"WWW-Authenticate": "Bearer"}
)
```

### Extra data

Include additional information in the error response:

```python
raise BadRequest(
    detail="Invalid input",
    extra={
        "field": "email",
        "value": "invalid@",
        "reason": "Invalid email format"
    }
)
```

Response:

```json
{
    "detail": "Invalid input",
    "extra": {
        "field": "email",
        "value": "invalid@",
        "reason": "Invalid email format"
    }
}
```

## Validation errors

### RequestValidationError

Raised when request validation fails. Django-Bolt's Serializer **automatically collects all validation errors** from `@field_validator` and `@model_validator` before raising, so users see all issues at once:

```python
from django_bolt.serializers import Serializer, field_validator

class UserSerializer(Serializer):
    email: str
    age: int

    @field_validator("email")
    def validate_email(cls, value):
        if "@" not in value:
            raise ValueError("Invalid email format")
        return value

    @field_validator("age")
    def validate_age(cls, value):
        if value < 0:
            raise ValueError("Must be positive")
        return value

# Both fields fail - ALL errors returned, not just the first
UserSerializer(email="invalid", age=-5)
# Raises RequestValidationError with both errors
```

You can also raise manually:

```python
from django_bolt.exceptions import RequestValidationError

errors = [
    {
        "loc": ["body", "email"],
        "msg": "Invalid email format",
        "type": "value_error"
    },
    {
        "loc": ["body", "age"],
        "msg": "Must be positive",
        "type": "value_error"
    }
]
raise RequestValidationError(errors)
```

Response (422 Unprocessable Entity):

```json
{
    "detail": [
        {
            "loc": ["body", "email"],
            "msg": "Invalid email format",
            "type": "value_error"
        },
        {
            "loc": ["body", "age"],
            "msg": "Must be positive",
            "type": "value_error"
        }
    ]
}
```

### ResponseValidationError

Raised when response validation fails (returns 500):

```python
from django_bolt.exceptions import ResponseValidationError

errors = [
    {
        "loc": ["response", "id"],
        "msg": "Field required",
        "type": "missing"
    }
]
raise ResponseValidationError(errors)
```

Response validation errors indicate a server bug, so they return 500 Internal Server Error.

## Error handlers

Django-Bolt provides built-in error handlers:

### http_exception_handler

Handles `HTTPException` and its subclasses:

```python
from django_bolt.error_handlers import http_exception_handler

exc = NotFound(detail="User not found")
status, headers, body = http_exception_handler(exc)
# status: 404
# body: b'{"detail": "User not found"}'
```

### request_validation_error_handler

Handles `RequestValidationError`:

```python
from django_bolt.error_handlers import request_validation_error_handler

errors = [{"loc": ["body"], "msg": "Invalid", "type": "value_error"}]
exc = RequestValidationError(errors)
status, headers, body = request_validation_error_handler(exc)
# status: 422
```

### generic_exception_handler

Handles unexpected exceptions:

```python
from django_bolt.error_handlers import generic_exception_handler

exc = ValueError("Something went wrong")

# Production mode - hide details
status, headers, body = generic_exception_handler(exc, debug=False)
# body: b'{"detail": "Internal Server Error"}'

# Debug mode - show Django error page
status, headers, body = generic_exception_handler(exc, debug=True)
# Returns HTML error page with traceback
```

### handle_exception

The main entry point that routes to appropriate handlers. It maps some Django exceptions as Django does:

- `PermissionDenied` gives 403.
- `SuspiciousOperation`, for example `DisallowedHost`, gives 400. Bolt logs it to the `django.security.<class name>` logger, as Django does. With `DEBUG=False`, the detail is `Bad Request`.

```python
from django_bolt.error_handlers import handle_exception

# Automatically detects exception type
status, headers, body = handle_exception(some_exception)
```

## Debug mode

In debug mode (`DEBUG=True` in Django settings), unhandled exceptions return Django's HTML error page with full traceback:

```python
# debug=True: Full HTML traceback page
status, headers, body = handle_exception(exc, debug=True)
# content-type: text/html

# debug=False: JSON error response
status, headers, body = handle_exception(exc, debug=False)
# content-type: application/json
```

The debug parameter defaults to Django's `DEBUG` setting.

## msgspec validation errors

Django-Bolt converts `msgspec.ValidationError` to the standard error format:

```python
from django_bolt.error_handlers import msgspec_validation_error_to_dict

try:
    msgspec.json.decode(b'{"age": "invalid"}', type=MyStruct)
except msgspec.ValidationError as e:
    errors = msgspec_validation_error_to_dict(e)
    # [{"loc": [...], "msg": "...", "type": "..."}]
```

## Error response format

All errors follow a consistent format:

```json
{
    "detail": "Error message",
    "extra": {}
}
```

For validation errors:

```json
{
    "detail": [
        {
            "loc": ["body", "field_name"],
            "msg": "Error message",
            "type": "error_type"
        }
    ]
}
```

## Error formatter

An error formatter changes the body of each error response that the framework makes. One function formats the errors from all layers:

- The Rust core:
  - 401 and 403 from guards, and 404 for no route.
  - 413, and 400 for bad headers.
  - 422 from path, query, header, cookie and form values.
  - 429 from rate limits.
  - A `FileResponse` whose file Rust cannot open or read when it sends the response.
- Python: request and response validation, `HTTPException`, and unhandled exceptions.

Without a formatter, the errors keep their default body.

### RFC 9457 Problem Details

Use the built-in `problem_details` formatter:

```python
from django_bolt import BoltAPI
from django_bolt.error_handlers import problem_details

api = BoltAPI(error_formatter=problem_details)
```

A request with no token to a route with `IsAuthenticated()` then gets this response:

```http
HTTP/1.1 401 Unauthorized
content-type: application/problem+json
www-authenticate: Bearer

{"type": "about:blank", "title": "Unauthorized", "status": 401,
 "detail": "Authentication required", "instance": "/private"}
```

An error with field errors also has `"errors"`, a list of `{"loc", "msg", "type"}` objects. Some objects also have `"ctx"`. The keys of `error.extra` become extension members, for example `"retry_after"` for a 429. A key does not replace a standard member. Bolt does not use an `"errors"` key of `error.extra`, because `"errors"` holds only field errors. The `title` is the RFC 9110 name of the status, on each Python version.

### Your own formatter

A formatter gets a `BoltError` and the request. It returns a `Response`, `JSON`, `PlainText` or `HTML`:

```python
from django_bolt import BoltAPI
from django_bolt.error_handlers import BoltError
from django_bolt.responses import Response


def format_error(error: BoltError, request) -> Response:
    body = {"code": error.kind, "message": error.detail}
    if error.errors:
        body["fields"] = error.errors
    return Response(body, status_code=error.status, media_type="application/vnd.error+json")


api = BoltAPI(error_formatter=format_error)
```

`BoltError` has these attributes:

| Attribute | Description |
|-----------|-------------|
| `status` | The HTTP status code. |
| `kind` | A stable name for the error. See the table below. |
| `detail` | A message for a human. Bolt does not put internal data, such as a file path, in it. |
| `errors` | The field errors. Each one has `loc`, `msg` and `type`. Some also have `ctx`. |
| `headers` | The headers that the error needs, for example `WWW-Authenticate` or `Retry-After`. |
| `extra` | The `extra` of an `HTTPException`, or `{"retry_after": n}` for a rate limit. |
| `exception` | The Python exception. It is `None` for an error of the Rust core. |

| Kind | Status | Source |
|------|--------|--------|
| `bad_request` | 400 | Too many headers, a header that is too large, a body that Bolt cannot read, or a Django `SuspiciousOperation`, such as `DisallowedHost` |
| `not_authenticated` | 401 | A guard, or an `HTTPException(401)` |
| `forbidden` | 403 | A guard, the CSRF origin check of Bolt, `PermissionDenied`, a `FileResponse` outside `BOLT_ALLOWED_FILE_PATHS` or an `HTTPException(403)` |
| `not_found` | 404 | No route, Django's `Http404` (also from `get_object_or_404`), a `FileResponse` or a `File` whose path names no regular file, or an `HTTPException(404)` |
| `payload_too_large` | 413 | A body over `BOLT_MAX_UPLOAD_SIZE` |
| `validation` | 422 | A value that fails validation, in Rust or in Python |
| `rate_limited` | 429 | A rate limit |
| `response_validation` | 500 | A handler return value that fails validation |
| `internal_error` | 500 | An unhandled exception, also a `FileNotFoundError` or `PermissionError` of handler code, a file in `BOLT_ALLOWED_FILE_PATHS` that the server cannot read, or a failure in the Rust core |

An `HTTPException` gets its kind from its status, for example `conflict` for 409. A status with no name gives `http_error`.

Bolt adds `error.headers` to the response of the formatter. A header that the response sets wins. Thus a 401 keeps `WWW-Authenticate`, and a 429 keeps `Retry-After`.

An `HTTPException` with a typed `body` keeps its body. The route declares that body, so the formatter does not get the error.

On the request of an error from the Rust core, `method`, `path`, `headers` and `get_full_path()` work. `query` is empty, so use `get_full_path()` to read the query string. The other request data is empty.

The formatter runs only for an error, and only when the app has a formatter. A request that succeeds does not call Python. With a formatter, each error of the Rust core calls Python, also a 404 or a 429. A flood of these errors then competes for the GIL with the other requests.

The formatter must be a sync callable. Bolt raises `TypeError` for an async one. The response of the formatter must have a status from 400 to 599. Each header name and value must be a `str`. The response cannot set cookies. When the formatter raises an exception or returns a response that is not valid, Bolt logs the error and sends the default response.

Bolt logs each error that it answers with a 500 by default, also when the formatter gives another status. Bolt does not log an error that it answers with a 4xx by default, also when the formatter gives a 5xx. An `HTTPException` is a planned response, so Bolt does not log it.

With more than one API in a project, `runbolt` uses one formatter for all of them. Set the same formatter on each API, or set it on one API. Two different formatters stop startup.

`mount()` follows the same rule. The parent API takes the formatter of the mounted API. Two different formatters raise `ValueError`.

The formatter runs on the thread of the request, also for an async handler. Keep it fast. Do not use the database or the network in it.

With a formatter, Bolt does not send the Django debug page for a 500 error when `DEBUG=True`. Use `error.exception` to show the details.

The formatter does not change a response that a handler or a middleware returns. It does not change the responses of a WebSocket handshake, an ASGI mount, an MCP mount, or a static file.

### OpenAPI schema

The OpenAPI schema shows a 422 response for each route with a request body. The body of this 422 follows the formatter of the API:

| Formatter | Media type | Schema |
|-----------|------------|--------|
| None | `application/json` | `{"detail": [...]}` |
| `problem_details` | `application/problem+json` | The `ProblemDetails` component, with `errors` |
| A formatter with `error_schema` | The declared media type | The declared type |
| A formatter without `error_schema` | None | None |

Use `error_schema` to declare the body of your own formatter:

```python
import msgspec

from django_bolt import BoltAPI
from django_bolt.error_handlers import BoltError, error_schema
from django_bolt.responses import Response


class ErrorBody(msgspec.Struct):
    code: str
    message: str
    fields: list[dict] = []


@error_schema(ErrorBody, media_type="application/vnd.error+json")
def format_error(error: BoltError, request) -> Response:
    body = {"code": error.kind, "message": error.detail, "fields": error.errors}
    return Response(body, status_code=error.status, media_type="application/vnd.error+json")


api = BoltAPI(error_formatter=format_error)
```

A formatter without `error_schema` gets a 422 with a description and no content. Bolt cannot know the media type or the shape of its body. A wrong media type or shape makes generated clients fail when they parse the error. A response with no content is valid OpenAPI and makes no claim about the body. `OpenAPIConfig(strict=True)` reports a formatter without `error_schema`.

The formatter does not change the entries of `response_model`. For example, `response_model={409: Conflict}` keeps its `application/json` schema. This agrees with the response, because an `HTTPException` with a typed `body` skips the formatter.

A route with a request body can declare its own 422, for example `response_model={422: Rejected}`. Such a route can send two 422 bodies: its own body and the validation body. The schema shows both, unless the formatter has no `error_schema`. Then the entry shows only the body of the route. When the two bodies have the same media type, the schema joins them with `anyOf`. The entry keeps the description of the route.

## Example: Custom error handling

```python
from django_bolt import BoltAPI
from django_bolt.exceptions import HTTPException, NotFound, BadRequest

api = BoltAPI()

@api.get("/users/{user_id}")
async def get_user(user_id: int):
    if user_id < 1:
        raise BadRequest(
            detail="Invalid user ID",
            extra={"user_id": user_id, "reason": "Must be positive"}
        )

    user = await User.objects.filter(id=user_id).afirst()
    if not user:
        raise NotFound(detail=f"User {user_id} not found")

    return {"id": user.id, "username": user.username}

@api.post("/orders")
async def create_order(data: OrderCreate):
    try:
        order = await Order.objects.acreate(**data.__dict__)
        return {"id": order.id}
    except IntegrityError:
        raise BadRequest(
            detail="Order creation failed",
            extra={"reason": "Duplicate order reference"}
        )
```

## Chaining exceptions

Preserve the original exception context:

```python
try:
    result = some_operation()
except ValueError as e:
    raise InternalServerError(detail=str(e))
```

The original traceback is preserved for debugging.

## Exception reference

### Client errors (4xx)

| Exception | Status Code | Default Message |
|-----------|-------------|-----------------|
| `BadRequest` | 400 | Bad Request |
| `Unauthorized` | 401 | Unauthorized |
| `Forbidden` | 403 | Forbidden |
| `NotFound` | 404 | Not Found |
| `MethodNotAllowed` | 405 | Method Not Allowed |
| `NotAcceptable` | 406 | Not Acceptable |
| `Conflict` | 409 | Conflict |
| `Gone` | 410 | Gone |
| `UnprocessableEntity` | 422 | Unprocessable Entity |
| `TooManyRequests` | 429 | Too Many Requests |

### Server errors (5xx)

| Exception | Status Code | Default Message |
|-----------|-------------|-----------------|
| `InternalServerError` | 500 | Internal Server Error |
| `BadGateway` | 502 | Bad Gateway |
| `ServiceUnavailable` | 503 | Service Unavailable |
| `GatewayTimeout` | 504 | Gateway Timeout |
