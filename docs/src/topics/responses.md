---
description: Return data, set status codes and headers, validate the response type, and send text, HTML, redirects, files, streams and cookies.
---

# Responses

A handler returns data or a response object. Bolt converts the return value to an HTTP response.
Return a `dict`, a `list` or a `msgspec.Struct` for JSON. Return a response class for other content.

```python title="blog/api.py"
import msgspec

from django_bolt import BoltAPI

api = BoltAPI()


class ArticleOut(msgspec.Struct):
    id: int
    title: str


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> ArticleOut:
    return ArticleOut(id=article_id, title="Hello")
```

```bash
curl -i localhost:8000/articles/1
```

```http
HTTP/1.1 200 OK
content-type: application/json

{"id":1,"title":"Hello"}
```

## Return data

Bolt chooses the content type from the type of the value:

| Return value | Content type | Body |
| --- | --- | --- |
| `dict`, `list` | `application/json` | JSON |
| `msgspec.Struct` | `application/json` | JSON object |
| A dataclass, on a route with that return type | `application/json` | JSON object |
| `int`, `float`, `bool`, `Decimal`, `UUID`, `datetime`, `date`, `time`, `Enum` | `application/json` | JSON value |
| `str` | `text/plain; charset=utf-8` | The text |
| `bytes` | `application/octet-stream` | The bytes |
| A `QuerySet` or a model instance, with a response type | `application/json` | See [Declare the response type](#declare-the-response-type) |
| `None`, on a route with `-> None` | `application/json` | `null` |

Bolt encodes `datetime` as ISO 8601, `Decimal` as a string and `UUID` as a string:

```python
import datetime
import decimal
import uuid


@api.get("/orders/{order_id}")
async def get_order(order_id: int) -> dict:
    return {
        "id": uuid.UUID(int=order_id),
        "total": decimal.Decimal("9.50"),
        "created_at": datetime.datetime(2026, 1, 2, 3, 4, 5),
    }
```

```json
{"id": "00000000-0000-0000-0000-000000000001", "total": "9.50", "created_at": "2026-01-02T03:04:05"}
```

A handler with no return annotation that returns `None` gets a 500 response, because Bolt cannot tell if a `return` is missing.

### Values in a JSON response

A dict, a list or a `msgspec.Struct` can contain these values:

| Value | JSON |
| --- | --- |
| `str`, `int`, `float`, `bool`, `None` | The same JSON value |
| `Decimal`, `Fraction` | A string, such as `"1.50"` or `"1/3"` |
| `UUID`, `Path`, an IP address or network, a compiled `re.Pattern`, `UserString` | A string |
| `date`, `time`, `datetime`, `timedelta` | An ISO 8601 string |
| An `Enum` member | Its value |
| `bytes`, `bytearray` | A base64 string |
| `dict`, `ChainMap`, `MappingProxyType`, `UserDict` | An object |
| `list`, `tuple`, `set`, `frozenset`, `deque`, `UserList`, `range`, `array.array`, a dict view | An array |
| A generator, a `map` object, a `zip` object | An array of the items that it gives |

A subclass of a type in the table has the JSON form of its base type. The items of a container follow the same rules.
On a route with a response type, Bolt validates first. A value must then match the type, so `-> list[int]` refuses a `range`.

Bolt refuses a value with no clear JSON form, such as a `ZoneInfo`, a `datetime.timezone`, a `SimpleNamespace` or a `complex`.
Django's `DjangoJSONEncoder` refuses these values too. The response is a 500 with `TypeError: Unsupported type`.
Convert such a value before you return it, for example `str(zone)` or `vars(namespace)`.

## Set the status code

The default status is 200. Set another default on the decorator:

```python
from django_bolt import status


@api.post("/articles", status_code=status.HTTP_201_CREATED)
async def create_article() -> dict:
    return {"id": 2, "title": "New"}
```

`status_code=201` is the same. The `django_bolt.status` module has a constant for each status, and the helpers `is_success()`, `is_redirect()`, `is_client_error()` and `is_server_error()`.

To choose the status in the handler, return a response object, such as `JSON(data, status_code=202)`.
A status must be from 200 to 599. A `status_code` outside this range raises `ValueError` at the decorator.

## Declare the response type

The return annotation is the response type. Bolt converts the return value to this type before it encodes it:

```python
from django.contrib.auth.models import User


class AuthorOut(msgspec.Struct):
    id: int
    username: str


@api.get("/authors")
async def list_authors() -> list[AuthorOut]:
    return User.objects.order_by("id")


@api.get("/authors/{author_id}")
async def get_author(author_id: int) -> AuthorOut:
    return await User.objects.aget(id=author_id)
```

```bash
curl localhost:8000/authors
```

```json
[{"id": 1, "username": "ana"}, {"id": 2, "username": "ben"}]
```

The conversion does three things:

- It reads model instances and QuerySets. A `QuerySet` loads only the fields of the type. Return the `QuerySet` and do not iterate it in an async handler.
- It removes fields that the type does not declare. Thus a handler cannot leak a field by accident. A response object that Bolt does not validate, for example `JSON(...)` with another status, keeps all its fields.
- It validates the value. A value that does not match gets a 500 response.

`response_model=` on the decorator sets the response type in place of the annotation:

```python
@api.get("/authors/{author_id}/profile", response_model=AuthorOut)
async def get_author_profile(author_id: int):
    return {"id": author_id, "username": "ana", "password": "hash"}
```

```json
{"id": 1, "username": "ana"}
```

### Turn off response validation

`validate_response=False` on the decorator sends the value with no conversion. `BoltAPI(validate_response=False)` sets the default for each route of the API.
The type then only documents the response in the [OpenAPI document](openapi.md).

### Validation of response objects

A `JSON` or `Response` object is validated only when its status is the default status of the route:

```python
from django_bolt import JSON


@api.post("/tags", status_code=201)
async def create_tag(name: str) -> AuthorOut:
    if name == "taken":
        return JSON({"name": ["Already taken."]}, status_code=400)  # not validated
    return JSON({"id": 1, "username": name}, status_code=201)  # validated as AuthorOut
```

### Document more than one status

Give `response_model` a `dict` from status code to type. Return a `(status, data)` tuple to choose the status:

```python
class Error(msgspec.Struct):
    detail: str


@api.get("/articles/{article_id}/summary", response_model={200: ArticleOut, 404: Error})
async def get_summary(article_id: int):
    if article_id != 1:
        return 404, {"detail": "Article not found"}
    return 200, {"id": 1, "title": "Hello"}
```

```bash
curl -i localhost:8000/articles/2/summary
```

```http
HTTP/1.1 404 Not Found
content-type: application/json

{"detail":"Article not found"}
```

Bolt does not validate the data of a status map. The map documents the responses in the OpenAPI document.

- A bare return value gets the `status_code` of the decorator, or else the lowest 2xx code of the map.
- `{204: None}` with `return 204, None` sends no body.
- A `...` key documents each status that the map does not list, for example `{200: ArticleOut, ...: Error}`.
- The data of the tuple can be a response object. Bolt sends it with the status of the tuple.

A `(status, data)` tuple works only on a route with a status map. On another route, it gets a 500 response.

## Return a response object

Use a response class to set the content type, the status or the headers.
Each class takes `status_code` and `headers`:

```python
from django_bolt import JSON, Response
from django_bolt.responses import HTML, PlainText, Redirect


@api.get("/health")
async def health():
    return PlainText("OK", headers={"Cache-Control": "no-store"})


@api.get("/welcome")
async def welcome():
    return HTML("<h1>Welcome</h1>")


@api.post("/articles/import")
async def import_articles():
    return JSON({"queued": 12}, status_code=202, headers={"X-Job-Id": "42"})


@api.get("/feed.xml")
async def feed():
    return Response("<rss/>", media_type="application/rss+xml")
```

```bash
curl -i localhost:8000/articles/import -X POST
```

```http
HTTP/1.1 202 Accepted
content-type: application/json
x-job-id: 42

{"queued":12}
```

| Class | Content type | Use |
| --- | --- | --- |
| `JSON(data)` | `application/json` | JSON with a status or headers |
| `PlainText(text)` | `text/plain; charset=utf-8` | Text |
| `HTML(html)` | `text/html; charset=utf-8` | HTML |
| `Response(content, media_type=...)` | `media_type`, default `application/json` | Any content type |
| `Redirect(url)` | (no body) | A redirect |
| `File(path)`, `FileResponse(path)` | From the file name | A file |
| `StreamingResponse(content)` | `media_type`, default `application/octet-stream` | A body in chunks |

`Response` encodes `content` as JSON when `media_type` is `application/json`. With another `media_type`, it sends `str` content as UTF-8 text.
It sends `bytes` content with no change for each media type.

!!! note
    Do not annotate a handler with `-> JSON` or `-> Response`. Bolt then validates the data against the class, and the response is a 500.
    Leave out the return annotation, or annotate the type of the data, as in [Validation of response objects](#validation-of-response-objects).

### Redirect

`Redirect` sends `307 Temporary Redirect` by default. The method and the body stay the same on the next request:

```python
@api.get("/posts/{article_id}")
async def old_article_url(article_id: int):
    return Redirect(f"/articles/{article_id}", status_code=301)
```

```http
HTTP/1.1 301 Moved Permanently
location: /articles/3
```

### Send a file

`File` reads the file into memory. `FileResponse` streams the file from disk, and Python does not read it.
Use `FileResponse` for large files:

```python
from django_bolt.responses import FileResponse


@api.get("/reports/{year}")
async def download_report(year: int):
    return FileResponse(f"/var/app/reports/{year}.csv", filename=f"report-{year}.csv")
```

```http
HTTP/1.1 200 OK
content-type: text/csv
content-disposition: attachment; filename="report-2026.csv"
```

- Bolt guesses the content type from the file name. Set `media_type` to choose it.
- `filename` adds a `Content-Disposition: attachment` header.
- A path that is not a regular file gets 404 `{"detail": "File not found"}`. The response does not show the path.

!!! warning
    A path that comes from the request can point at any file that the server can read.
    Set `BOLT_ALLOWED_FILE_PATHS` to a list of absolute directories. `FileResponse` then sends only files in them.
    A path outside them gets 403 `{"detail": "Permission denied"}`. `File` does not check this setting.

### Stream a response

Give `StreamingResponse` a generator or an async generator. Bolt sends each item as a chunk:

```python
from django_bolt import StreamingResponse


@api.get("/articles/export.csv")
async def export_csv():
    async def rows():
        yield "id,title\n"
        for article_id in range(1, 4):
            yield f"{article_id},Article {article_id}\n"

    return StreamingResponse(rows(), media_type="text/csv")
```

```text
id,title
1,Article 1
2,Article 2
3,Article 3
```

Pass the generator object, `rows()`, not the function `rows`. A function raises `TypeError`.
Bolt sends the status and headers before the first chunk. An exception in the generator thus ends the body early, and the status stays 200.
Bolt logs the exception to the `django_bolt.streaming` logger.

A handler can also be an async generator. Annotate it with `AsyncIterator[T]`, and Bolt sends each item as one JSON line:

```python
from collections.abc import AsyncIterator


@api.get("/articles/stream")
async def stream_articles() -> AsyncIterator[ArticleOut]:
    for article_id in range(1, 3):
        yield ArticleOut(id=article_id, title=f"Article {article_id}")
```

```text
{"id":1,"title":"Article 1"}
{"id":2,"title":"Article 2"}
```

The content type is `application/x-ndjson`.

For Server-Sent Events, see [Server-Sent Events](sse.md).

## Set headers

Pass `headers` to a response class:

```python
@api.get("/articles/{article_id}/cached")
async def cached_article(article_id: int):
    return JSON({"id": article_id}, headers={"Cache-Control": "max-age=60"})
```

Each header value must be a `str`. A value of another type gets a 500 response.

Each response copies its `headers` dict. A change to `response.headers` does not change the dict that you give.
Thus many responses can share one module-level dict:

```python
COMMON_HEADERS = {"X-Service": "blog"}


@api.get("/articles/{article_id}/counted")
async def counted_article(article_id: int):
    response = JSON({"id": article_id}, headers=COMMON_HEADERS)
    response.headers["X-Count"] = "1"  # COMMON_HEADERS does not change
    return response
```

## Set and delete cookies

Each response class has `set_cookie()` and `delete_cookie()`. Each call returns the response, so you can chain calls:

```python
@api.post("/sessions")
async def start_session():
    return (
        JSON({"ok": True})
        .set_cookie("session", "abc123", max_age=3600, httponly=True, secure=True)
        .set_cookie("theme", "dark")
    )


@api.delete("/sessions")
async def end_session():
    return JSON({"ok": True}).delete_cookie("session")
```

```http
HTTP/1.1 200 OK
set-cookie: session=abc123; HttpOnly; SameSite=Lax; Secure; Path=/; Max-Age=3600
set-cookie: theme=dark; SameSite=Lax; Path=/
```

`set_cookie()` takes the arguments of Django's `HttpResponse.set_cookie()`: `name`, `value`, `max_age`, `expires`, `path`, `domain`, `secure`, `httponly` and `samesite`.
The default `samesite` is `"Lax"`. Pass `samesite=False` to leave out the attribute. Another value than `"Strict"`, `"Lax"` or `"None"` raises `ValueError`.

`delete_cookie(name, path="/", domain=None)` sends the cookie with `Max-Age=0`. Give the `path` and `domain` that you set the cookie with.

`django_bolt.Cookie` is the class of a response cookie. It is not the `Cookie()` marker of `django_bolt.params`, which reads a request cookie.

## Render a template

`django_bolt.shortcuts.render()` renders a Django template, as `django.shortcuts.render()` does:

```python
from django_bolt import Request
from django_bolt.shortcuts import render


@api.get("/articles/{article_id}/page")
async def article_page(request: Request, article_id: int):
    article = {"title": "Hello", "body": "First post"}
    return render(request, "blog/article.html", {"article": article})
```

```html title="blog/templates/blog/article.html"
<h1>{{ article.title }}</h1>
<p>{{ article.body }}</p>
```

The response is `text/html; charset=utf-8`. Pass `content_type` and `status` to change them.
A template that reads `{{ user }}` loads the user with a blocking query. In an async handler, call `await request.auser()` before `render()`.

### Render a template later

`render()` renders the template at once. To render it after the handler returns, return a `TemplateResponse`.
It is the `TemplateResponse` class of Django, with the same arguments: `status`, `content_type` and `headers`.

```python
from django_bolt import Request
from django_bolt.responses import TemplateResponse


@api.get("/articles/{article_id}/preview")
async def article_preview(request: Request, article_id: int):
    article = {"title": "Hello", "body": "First post"}
    return TemplateResponse(request, "blog/article.html", {"article": article})
```

On a route with Django middleware, Bolt first calls the `process_template_response` hook of each middleware.
A hook can change `template_name` and `context_data` before the render. See [Middleware](middleware.md#run-django-middleware).
A route with no Django middleware renders the response with no hooks.

The render can read a QuerySet in the context, also in an async handler. Bolt renders where a sync ORM call can run.

## Send an empty response

Return `None` on a route with a 204 status:

```python
@api.delete("/articles/{article_id}", status_code=204)
async def delete_article(article_id: int) -> None:
    return None
```

A 204 response has no body.

## See also

- [Responses reference](../ref/responses.md)
- [Error handling](error-handling.md)
- [Serializers](serializers.md)
- [Async and the ORM](async-orm.md)
- [Server-Sent Events](sse.md)
- [Compression](compression.md)
