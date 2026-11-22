---
description: Raise HTTP errors from handlers, read the 422 and 500 error bodies, map your own exceptions, and change the error format.
---

# Error handling

Raise an `HTTPException`, or one of its subclasses, to stop a handler and send an error response.
Bolt also turns validation errors, Django exceptions and unhandled exceptions into JSON error responses.

```python title="blog/api.py"
from django_bolt import BoltAPI
from django_bolt.exceptions import NotFound

api = BoltAPI()


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    if article_id != 1:
        raise NotFound(f"Article {article_id} not found")
    return {"id": 1, "title": "Hello"}
```

```bash
curl -i localhost:8000/articles/7
```

```http
HTTP/1.1 404 Not Found
content-type: application/json

{"detail":"Article 7 not found"}
```

## Raise an HTTP error

`django_bolt.exceptions` has one class for each common error status:

| Class | Status |
| --- | --- |
| `BadRequest` | 400 |
| `Unauthorized` | 401 |
| `Forbidden` | 403 |
| `NotFound` | 404 |
| `MethodNotAllowed` | 405 |
| `NotAcceptable` | 406 |
| `Conflict` | 409 |
| `Gone` | 410 |
| `UnprocessableEntity` | 422 |
| `TooManyRequests` | 429 |
| `InternalServerError` | 500 |
| `NotImplemented` | 501 |
| `BadGateway` | 502 |
| `ServiceUnavailable` | 503 |
| `GatewayTimeout` | 504 |

For another status, raise `HTTPException` with the status:

```python
from django_bolt.exceptions import HTTPException

raise HTTPException(status_code=451, detail="Unavailable in your country")
```

The first argument of a subclass is the `detail`. `NotFound("Article not found")` is the same as `NotFound(detail="Article not found")`.
Without a detail, the detail is the standard phrase of the status, for example `"Not Found"`.

### Send structured data

A `dict` or a `list` detail goes to the client as JSON:

```python
from django_bolt.exceptions import Conflict


@api.post("/articles/{slug}")
async def create_article(slug: str) -> dict:
    raise Conflict(detail={"code": "duplicate_slug", "slug": slug})
```

```json
{"detail": {"code": "duplicate_slug", "slug": "hello"}}
```

An empty `dict` or `list` is the same as no detail. The client gets the standard phrase of the status.

`extra` adds an `extra` key next to `detail`:

```python
from django_bolt.exceptions import BadRequest


@api.get("/reports")
async def reports(start: str, end: str) -> dict:
    raise BadRequest("Invalid date range", extra={"start": start, "end": end})
```

```json
{"detail": "Invalid date range", "extra": {"start": "2026-02-01", "end": "2026-01-01"}}
```

### Add headers

`headers` adds response headers. A number value becomes a string:

```python
from django_bolt.exceptions import TooManyRequests


@api.post("/articles/{article_id}/publish")
async def publish_article(article_id: int) -> dict:
    raise TooManyRequests("Publish limit reached", headers={"Retry-After": 30})
```

```http
HTTP/1.1 429 Too Many Requests
content-type: application/json
retry-after: 30

{"detail":"Publish limit reached"}
```

### Send a typed error body

`body` replaces the `{"detail": ...}` object. Declare its type in `response_model` so that the OpenAPI document shows it:

```python
import msgspec


class Rejected(msgspec.Struct):
    reason: str
    retry: bool


@api.post("/articles/{article_id}/review", response_model={200: dict, 409: Rejected})
async def submit_for_review(article_id: int) -> dict:
    raise HTTPException(409, body=Rejected(reason="Already in review", retry=False))
```

```json
{"reason": "Already in review", "retry": false}
```

Bolt encodes `body` with no validation.

## Validation errors

A request value that fails validation gets a 422 response. The handler does not run.
[Requests](requests.md#validation-errors) shows the two forms of the body.

To report your own validation error in the same form, raise `RequestValidationError` with a list of errors:

```python
from django_bolt.exceptions import RequestValidationError


@api.post("/tags")
async def create_tag(name: str) -> dict:
    if name == "django":
        raise RequestValidationError(
            [{"loc": ["query", "name"], "msg": "Tag already exists", "type": "value_error"}]
        )
    return {"name": name}
```

```json
{"detail": [{"loc": ["query", "name"], "msg": "Tag already exists", "type": "value_error"}]}
```

## Response validation errors

A return value that does not match the response type of the route gets a 500 response.
The client sees only this body when `DEBUG` is `False`:

```json
{"detail": "Response validation error"}
```

When `DEBUG` is `True`, the body also has the errors:

```json
{"detail": "Response validation error", "extra": {"validation_errors": [{"loc": ["response", "id"], "msg": "Response validation failed for ArticleOut: Expected `int`, got `str`", "type": "validation_error"}]}}
```

Bolt always logs the errors. See [Responses](responses.md#declare-the-response-type).

## Django exceptions

Bolt answers the Django exceptions as Django does:

| Exception | Status | `detail` |
| --- | --- | --- |
| `django.http.Http404`, also from `get_object_or_404()` | 404 | `"Not Found"`. The message when `DEBUG` is `True` |
| `django.core.exceptions.PermissionDenied` | 403 | The message, or `"Permission denied"` |
| `django.core.exceptions.SuspiciousOperation`, for example `DisallowedHost` | 400 | `"Bad Request"`. The message when `DEBUG` is `True` |

```python
from django.contrib.auth.models import User
from django.shortcuts import aget_object_or_404


@api.get("/authors/{author_id}")
async def get_author(author_id: int) -> dict:
    author = await aget_object_or_404(User, id=author_id)
    return {"username": author.username}
```

```json
{"detail": "Not Found"}
```

## Unhandled exceptions

Bolt logs an unhandled exception with its traceback and sends a 500 response.

- When `DEBUG` is `False`, the body is `{"detail": "Internal Server Error"}`. The client does not see the exception.
- When `DEBUG` is `True`, the body is the Django debug page, as HTML.

A `FileNotFoundError` or a `PermissionError` of your own code is also a 500. Only the file responses of Bolt answer 404 or 403 for a missing or forbidden file.

## Map your own exceptions to responses

Catch an exception in a middleware and return a response. The middleware applies to each route of the API:

```python
from django_bolt import JSON
from django_bolt.middleware import BaseMiddleware


class ArticleLocked(Exception):
    pass


class ArticleLockedMiddleware(BaseMiddleware):
    async def process_request(self, request):
        try:
            return await self.get_response(request)
        except ArticleLocked as exc:
            return JSON({"detail": str(exc)}, status_code=423)


api = BoltAPI(middleware=[ArticleLockedMiddleware])


@api.put("/articles/{article_id}")
async def edit_article(article_id: int) -> dict:
    raise ArticleLocked(f"Article {article_id} is locked for editing")
```

```json
{"detail": "Article 7 is locked for editing"}
```

A Python middleware runs for each request of its routes. See [Middleware](middleware.md).

## Change the error format

`BoltAPI(error_formatter=...)` sets one function that formats each error response that Bolt makes.
This covers the errors of the Rust layer (404, 405, 401 and 403 from guards, 422, 429) and of Python (`HTTPException`, validation, unhandled exceptions).

### Use RFC 9457 problem details

```python
from django_bolt import BoltAPI
from django_bolt.error_handlers import problem_details

api = BoltAPI(error_formatter=problem_details)
```

```bash
curl -i localhost:8000/articles/x
```

```http
HTTP/1.1 422 Unprocessable Entity
content-type: application/problem+json

{"type":"about:blank","title":"Unprocessable Content","status":422,"detail":"Request validation failed","instance":"/articles/x","errors":[{"loc":["path","article_id"],"msg":"Invalid integer 'x': invalid digit found in string","type":"validation_error"}]}
```

The body has `type`, `title`, `status`, `detail` and `instance`. It has `errors` when the error has field errors.
The keys of `extra` become more members of the body.
`detail` is always a string. The keys of a `dict` detail become more members, and `detail` is then the title.

### Write a formatter

A formatter takes a `BoltError` and the request. It returns a `Response`, `JSON`, `PlainText` or `HTML`:

```python
from django_bolt import BoltAPI
from django_bolt.error_handlers import BoltError
from django_bolt.responses import Response


def format_error(error: BoltError, request) -> Response:
    body = {"code": error.kind, "message": error.detail}
    if error.errors:
        body["fields"] = error.errors
    return Response(body, status_code=error.status)


api = BoltAPI(error_formatter=format_error)
```

```json
{"code": "not_found", "message": "Article 7 not found"}
```

`BoltError` has these attributes:

| Attribute | Value |
| --- | --- |
| `status` | The HTTP status code |
| `kind` | A fixed name, for example `"not_found"`, `"validation"`, `"rate_limited"` or `"internal_error"` |
| `detail` | A message for a person. A `dict` or `list` when an `HTTPException` has one |
| `errors` | The field errors. Each one has `loc`, `msg` and `type` |
| `headers` | Headers that the error needs, for example `WWW-Authenticate` |
| `extra` | The `extra` of an `HTTPException` |
| `exception` | The Python exception, or `None` for an error of the Rust layer |

Rules for a formatter:

- It must be a sync function. An `async` function raises `TypeError`.
- It must return a status from 400 to 599. It cannot set cookies.
- Bolt adds `error.headers` to the response. A header that the response sets wins.
- When the formatter raises or returns a bad response, Bolt logs the problem and sends the default response.
- An `HTTPException` with a `body`, or with a status below 400, does not go through the formatter.
- With a formatter, Bolt does not send the Django debug page.
- With more than one API, set the same formatter on each, or on one. Two different formatters stop the startup.

To show the body of your formatter in the OpenAPI document, decorate it with `error_schema`. See the [exceptions reference](../ref/exceptions.md#error-formatters).

## See also

- [Exceptions reference](../ref/exceptions.md)
- [Requests](requests.md#validation-errors)
- [Responses](responses.md)
- [Middleware](middleware.md)
- [Logging](logging.md)
