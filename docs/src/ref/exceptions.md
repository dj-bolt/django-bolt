---
description: Reference for the exception classes in django_bolt.exceptions and for the error formatter API in django_bolt.error_handlers.
---

# Exceptions

Raise an `HTTPException` subclass in a handler to send an error response.
For a task-based introduction, see [Error handling](../topics/error-handling.md).

```python
from django_bolt.exceptions import HTTPException, NotFound
```

**Example**

```python
from django_bolt import BoltAPI
from django_bolt.exceptions import NotFound

api = BoltAPI()


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    raise NotFound(f"Article {article_id} not found", headers={"Cache-Control": "no-store"})
```

```http
HTTP/1.1 404 Not Found
cache-control: no-store
content-type: application/json

{"detail":"Article 7 not found"}
```

## HTTPException

<!-- fragment -->
```python
HTTPException(status_code=None, detail=None, headers=None, extra=None, *, body=None)
```

The base class of the HTTP errors. Bolt answers it with its status and the body `{"detail": detail}`, or `{"detail": detail, "extra": extra}`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `status_code` | `int \| str \| None` | `None` | The status. `None` uses the `status_code` of the class, which is 500 for `HTTPException`. A `str` in this position is the `detail`. |
| `detail` | `Any` | `None` | The message. A `dict` or a `list` goes to the client as JSON. Another value becomes its `str()`. An empty value gives the standard phrase of the status. |
| `headers` | `dict[str, str] \| None` | `None` | Response headers. An `int` or `float` value becomes a `str`. |
| `extra` | `dict \| list \| None` | `None` | Data for the `extra` key of the body. |
| `body` | `Any` | `None` | A body that replaces the `{"detail": ...}` object, for example a `msgspec.Struct`. Bolt encodes it with no validation. |

**Attributes**

`status_code`, `detail`, `headers`, `extra` and `body`. `detail` holds the standard phrase when you give no detail.

**Errors**

- `TypeError`: the first argument is a `str` and `detail` is also set.
- `TypeError`: a header name is not a `str`, or a header value is not a `str` or a number.

A status from 200 to 399 is not an error. Bolt sends it with the same body, and the error formatter does not change it.
A status outside 200 to 599 gets a 500 response.

## Client errors

Each class is an `HTTPException` with a fixed `status_code`. It takes the arguments of `HTTPException`.
The first argument is the `detail`: `NotFound("Article not found")`.

| Class | Status | Default `detail` |
| --- | --- | --- |
| `ClientException` | 400 | `"Bad Request"` |
| `BadRequest` | 400 | `"Bad Request"` |
| `Unauthorized` | 401 | `"Unauthorized"` |
| `Forbidden` | 403 | `"Forbidden"` |
| `NotFound` | 404 | `"Not Found"` |
| `MethodNotAllowed` | 405 | `"Method Not Allowed"` |
| `NotAcceptable` | 406 | `"Not Acceptable"` |
| `Conflict` | 409 | `"Conflict"` |
| `Gone` | 410 | `"Gone"` |
| `UnprocessableEntity` | 422 | `"Unprocessable Content"`. On Python 3.12 it is `"Unprocessable Entity"` |
| `TooManyRequests` | 429 | `"Too Many Requests"` |

`ClientException` is the base class of the other classes in this table.

## Server errors

| Class | Status | Default `detail` |
| --- | --- | --- |
| `ServerException` | 500 | `"Internal Server Error"` |
| `InternalServerError` | 500 | `"Internal Server Error"` |
| `NotImplemented` | 501 | `"Not Implemented"` |
| `BadGateway` | 502 | `"Bad Gateway"` |
| `ServiceUnavailable` | 503 | `"Service Unavailable"` |
| `GatewayTimeout` | 504 | `"Gateway Timeout"` |

`ServerException` is the base class of the other classes in this table.
`NotImplemented` has the name of a Python built-in constant. Import the module to keep both names: `from django_bolt import exceptions`, then `raise exceptions.NotImplemented()`.

## RequestValidationError

<!-- fragment -->
```python
RequestValidationError(errors, *, body=None)
```

A request value is not valid. Bolt answers 422 with `{"detail": errors}`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `errors` | `Sequence` | (required) | The errors. Each error is a `dict` with `loc`, `msg` and `type`. |
| `body` | `Any` | `None` | The request body that is not valid. |

`exc.errors()` returns `errors`. `str(exc)` joins the `loc` and `msg` of each error.

## ResponseValidationError

<!-- fragment -->
```python
ResponseValidationError(errors, *, body=None)
```

A return value does not match the response type of the route. Bolt raises it. Bolt answers 500 with `{"detail": "Response validation error"}`.
When `DEBUG` is `True`, the body also has `extra.validation_errors`. Bolt always logs the errors.

The parameters are the parameters of `RequestValidationError`.

## ValidationException

<!-- fragment -->
```python
ValidationException(errors)
```

The base class of `RequestValidationError` and `ResponseValidationError`. It is a `ValueError`.
Bolt answers an instance of this class itself as a `RequestValidationError`.

## BoltException

<!-- fragment -->
```python
BoltException(*args, detail="")
```

The base class of each exception in this module, except the file errors. The first non-empty argument is the `detail` when you do not give `detail`.

## SerializationError

A `Serializer` is used in a way that it does not support. It is a `BoltException` and a `ValueError`. Bolt answers it with a 500 response.

`UnloadedRelationError` is a `SerializationError`. A sync serializer needs a Django relation that is not loaded. See [Serializers](../topics/serializers.md).

## ResponseFileNotFound

The path of a `FileResponse` or a `File` names no regular file. It is a `FileNotFoundError`. Bolt answers 404 with `{"detail": "File not found"}`.
A `FileNotFoundError` of your own code gets a 500 response.

## ResponseFileNotAllowed

The path of a `FileResponse` is outside `BOLT_ALLOWED_FILE_PATHS`. It is a `PermissionError`. Bolt answers 403 with `{"detail": "Permission denied"}`.
A `PermissionError` of your own code gets a 500 response.

## Other exceptions

Bolt answers these exceptions of Django and msgspec:

| Exception | Status | `detail` |
| --- | --- | --- |
| `django.http.Http404` | 404 | `"Not Found"`. The message when `DEBUG` is `True` |
| `django.core.exceptions.PermissionDenied` | 403 | The message, or `"Permission denied"` |
| `django.core.exceptions.SuspiciousOperation` | 400 | `"Bad Request"`. The message when `DEBUG` is `True` |
| `msgspec.ValidationError` | 422 | A list of errors |
| Each other exception | 500 | `"Internal Server Error"`. The Django debug page when `DEBUG` is `True` |

## Error formatters

`BoltAPI(error_formatter=...)` takes a function that formats each error response of the framework. These names are in `django_bolt.error_handlers`.

```python
from django_bolt.error_handlers import BoltError, error_schema, problem_details
```

### BoltError

<!-- fragment -->
```python
BoltError(*, status, kind, detail, errors=[], headers={}, extra=None, exception=None)
```

The error that the formatter gets. It is a frozen `msgspec.Struct`.

| Field | Type | Description |
| --- | --- | --- |
| `status` | `int` | The HTTP status. |
| `kind` | `str` | A fixed name for the error. See the table below. |
| `detail` | `str \| dict \| list` | A message for a person. A `dict` or `list` when an `HTTPException` has one. |
| `errors` | `list[dict]` | The field errors. Each one has `loc`, `msg` and `type`, and some have `ctx`. A `response_validation` error has none when `DEBUG` is `False`. |
| `headers` | `dict[str, str]` | Headers that the error needs, for example `WWW-Authenticate` or `Retry-After`. Bolt adds them to the response of the formatter, unless the response sets the same header. |
| `extra` | `Any` | The `extra` of an `HTTPException`, or `{"retry_after": n}` for a rate limit. |
| `exception` | `BaseException \| None` | The Python exception. `None` for an error of the Rust layer. |

| `kind` | Status | Source |
| --- | --- | --- |
| `bad_request` | 400 | A bad request, or a `SuspiciousOperation` |
| `not_authenticated` | 401 | A guard, or `HTTPException(401)` |
| `forbidden` | 403 | A guard, `PermissionDenied`, `ResponseFileNotAllowed`, or `HTTPException(403)` |
| `not_found` | 404 | No route, `Http404`, `ResponseFileNotFound`, or `HTTPException(404)` |
| `method_not_allowed` | 405 | No route for the method, or `HTTPException(405)` |
| `payload_too_large` | 413 | A body over `BOLT_MAX_UPLOAD_SIZE` |
| `validation` | 422 | A request value that is not valid |
| `rate_limited` | 429 | A rate limit |
| `response_validation` | 500 | A return value that does not match the response type |
| `internal_error` | 500 | An unhandled exception |
| `gateway_timeout` | 504 | A [native route](../topics/native-routes.md) that does not complete in time |

An `HTTPException` gets the kind of its status: `not_acceptable` (406), `conflict` (409), `gone` (410), `not_implemented` (501), `bad_gateway` (502), `service_unavailable` (503), or one of the kinds above. Another status gives `http_error`.

### The formatter function

<!-- fragment -->
```python
def formatter(error: BoltError, request) -> Response | JSON | PlainText | HTML: ...
```

- It must be a sync function. `BoltAPI` raises `TypeError` for an async function.
- The response must have a status from 400 to 599. It cannot set cookies. Each header name and value must be a `str`.
- When the formatter raises or returns a response that is not valid, Bolt logs the problem and sends the default response.
- For an error of the Rust layer, `request.method`, `request.path`, `request.headers` and `request.get_full_path()` work. `request.query` is empty.

### problem_details()

<!-- fragment -->
```python
problem_details(error: BoltError, request) -> Response
```

A formatter for RFC 9457 Problem Details. The content type is `application/problem+json`.
The body has `type` (`"about:blank"`), `title`, `status`, `detail` and `instance` (the request path).
It has `errors` when the error has field errors. The keys of `error.extra` are more members. A key does not replace a standard member.
`detail` is a string. A `dict` detail goes to more members, a `list` detail goes to an `extra` member, and `detail` is then the title.

The OpenAPI document shows the body as the `ProblemDetails` schema, with `ProblemFieldError` items.

### error_schema()

<!-- fragment -->
```python
error_schema(schema, *, media_type="application/json")
```

A decorator for your formatter. The OpenAPI document then shows `schema` and `media_type` for the error responses.
Without it, the 422 response of a route with a custom formatter has no content in the OpenAPI document.

```python
import msgspec

from django_bolt import BoltAPI
from django_bolt.error_handlers import BoltError, error_schema
from django_bolt.responses import Response


class ErrorBody(msgspec.Struct):
    code: str
    message: str


@error_schema(ErrorBody)
def format_error(error: BoltError, request) -> Response:
    return Response({"code": error.kind, "message": str(error.detail)}, status_code=error.status)


api = BoltAPI(error_formatter=format_error)
```

## See also

- [Error handling](../topics/error-handling.md)
- [Responses reference](responses.md)
- [BoltAPI reference](api.md)
- [OpenAPI](../topics/openapi.md)
