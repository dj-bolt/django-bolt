---
description: Read path, query, header, cookie, form, file and JSON body values from typed handler arguments, and use the Request object.
---

# Requests

Bolt gives each argument of a handler a value from the request.
The name and the type hint of the argument set where the value comes from and its type.
Bolt converts and validates each value before the handler runs. A bad value gets a 422 response.

```python title="blog/api.py"
import msgspec

from django_bolt import BoltAPI

api = BoltAPI()


class ArticleIn(msgspec.Struct):
    title: str
    body: str
    published: bool = False


@api.post("/authors/{author_id}/articles")
async def create_article(author_id: int, article: ArticleIn, notify: bool = False) -> dict:
    return {
        "author_id": author_id,
        "title": article.title,
        "published": article.published,
        "notify": notify,
    }
```

```bash
curl -X POST "localhost:8000/authors/7/articles?notify=true" \
  -H "Content-Type: application/json" -d '{"title": "Hello", "body": "First post"}'
```

```json
{"author_id": 7, "title": "Hello", "published": false, "notify": true}
```

`author_id` is in the path, so it is a path parameter.
`article` is a `msgspec.Struct`, so it is the JSON body.
`notify` is a simple type, so it is a query parameter.

## How Bolt finds the source of an argument

Bolt applies these rules in order:

| The argument | Source |
| --- | --- |
| Has a marker from `django_bolt.params`, such as `Header()` | The source of the marker |
| Has a name that is in the path, such as `{author_id}` | Path |
| Is named `request` or `req` | The [`Request` object](#use-the-request-object) |
| Has a simple type (`str`, `int`, `float`, `bool`, `Decimal`, `UUID`, a date or time type, an `Enum`, a `Literal`), or a list of a simple type | Query string |
| Is a `msgspec.Struct`, a dataclass, or a list of them | JSON body |
| Is a `dict`, or another type that only JSON can hold, on a `POST`, `PUT` or `PATCH` route | JSON body |
| Anything else | Query string |

Put a marker in `Annotated` to choose the source:

```python
from typing import Annotated

from django_bolt.params import Header


@api.get("/whoami")
async def whoami(user_agent: Annotated[str, Header(alias="User-Agent")]) -> dict:
    return {"user_agent": user_agent}
```

The markers are `Path`, `Query`, `Header`, `Cookie`, `Form`, `File` and `Body`. See the [parameters reference](../ref/params.md).

A type that Bolt cannot build from text raises `TypeError` when you register the route.
An example is a `dict` query parameter on a `GET` route. The error names the handler, the argument and the type.

## Read path parameters

A path parameter has the name of a `{name}` part of the path. The type hint sets its type:

```python
@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    return {"id": article_id}
```

Add limits with `Path()`:

```python
from django_bolt.params import Path


@api.get("/articles/{article_id}/revisions/{number}")
async def get_revision(article_id: int, number: Annotated[int, Path(ge=1)]) -> dict:
    return {"article_id": article_id, "number": number}
```

```bash
curl localhost:8000/articles/3/revisions/0
```

```json
{"detail": [{"type": "validation_error", "loc": ["path", "number"], "msg": "Expected `int` >= 1", "input": 0}]}
```

A path parameter is always required. `Path(default=...)` raises `ValueError`.
[Routing](routing.md#read-path-parameters) shows the `{name:path}` form, which matches slashes.

## Read query parameters

An argument with a simple type is a query parameter. Give it a default to make it optional:

```python
@api.get("/articles")
async def list_articles(page: int = 1, search: str | None = None, tag: list[str] | None = None) -> dict:
    return {"page": page, "search": search, "tag": tag}
```

```bash
curl "localhost:8000/articles?page=2&search=django&tag=python&tag=web"
```

```json
{"page": 2, "search": "django", "tag": ["python", "web"]}
```

- An argument with no default is required. A request without it gets a 422.
- A `list`, `set`, `frozenset` or `tuple` takes each value of a repeated key. Another type takes the last value.
- A `bool` takes `true`, `false`, `1`, `0`, `yes`, `no`, `on` and `off`.
- A `+` in the query string is a space, as in Django. Send a literal `+` as `%2B`.

### Send typed values as text

Bolt converts the text of a path, query, header, cookie or form value to the type of the argument.
It accepts the text that msgspec accepts. Thus a single argument and a `list[...]` argument of one type agree.

| Type | Text | Examples |
| --- | --- | --- |
| `int` | Digits with an optional leading `-`, at most 4300 characters. A whole number in float form up to `2**53`. | `42`, `-7`, `9223372036854775808`, `5.0`, `1e3` |
| `float` | Digits with an optional leading `-`, fraction and exponent. `nan`, `inf` or `infinity` in any case. | `1.5`, `-2e3`, `inf` |
| `date` | `YYYY-MM-DD` | `2024-01-15` |
| `time` | `HH:MM:SS`, then an optional fraction, then an optional `Z` or `±HH:MM` offset. | `10:30:00`, `10:30:00.5+05:00` |
| `datetime` | A `date`, then `T`, `t` or a space, then a `time`. Or a Unix timestamp. | `2024-01-15T10:30:00Z`, `2024-01-15 10:30:00+0530`, `1700000000` |

- A `datetime` or a `time` keeps the offset of its text. A text with no offset gives a naive value. A Unix timestamp gives a `datetime` in UTC.
- A fraction rounds to the closest microsecond.
- An offset can have no colon, as in `+0530`.
- Bolt refuses a leading `+`, a leading zero (`007`), `_`, whitespace and a field of another width (`2024-1-5`).
- Bolt refuses the other ISO 8601 forms, for example `2024-01-15T10:30` with no seconds. An HTML `<input type="datetime-local">` sends this form. To accept it, declare the argument as `str` and call `datetime.fromisoformat()`.

### Rename and limit a query parameter

Use `Query()` to read another key or to add limits:

```python
from django_bolt.params import Query


@api.get("/search")
async def search(
    q: Annotated[str, Query(alias="query", min_length=3)],
    page: Annotated[int, Query(ge=1)] = 1,
) -> dict:
    return {"q": q, "page": page}
```

```bash
curl "localhost:8000/search?query=dj"
```

```json
{"detail": [{"type": "validation_error", "loc": ["query", "query"], "msg": "Expected `str` of length >= 3", "input": "dj"}]}
```

`Query()` takes `gt`, `ge`, `lt`, `le`, `min_length`, `max_length` and `pattern`.
A `msgspec.Meta` in `Annotated` works the same way, for example `Annotated[int, msgspec.Meta(ge=1)]`.
A `Literal` takes only its values, and an `Enum` takes only the values of its members.

Write the default after `=`, not in the marker. `Annotated[str, Query("x")]` raises `TypeError`.
The marker can also be the default: `page: int = Query(1, ge=1)`.

## Read a JSON body

A `msgspec.Struct` argument reads the JSON body. Bolt decodes and validates it in one step:

```python
class ArticleIn(msgspec.Struct):
    title: str
    body: str
    published: bool = False


@api.post("/articles")
async def create_article(article: ArticleIn) -> dict:
    return {"title": article.title, "published": article.published}
```

A dataclass works the same way. A `list[ArticleIn]` reads a JSON array.
A `GET`, `HEAD`, `DELETE` or `OPTIONS` route cannot have a body argument. Bolt raises `TypeError` when you register it.

A request with no body gets a 422. Give the argument a default to make the body optional:

```python
@api.post("/articles/preview")
async def preview(article: ArticleIn | None = None) -> dict:
    return {"has_article": article is not None}
```

### Read a JSON object with no fixed fields

A `dict` argument on a `POST`, `PUT` or `PATCH` route reads a JSON object:

```python
@api.post("/events")
async def create_event(payload: dict) -> dict:
    return {"keys": sorted(payload)}
```

`dict[str, int]` also validates the keys and the values.

### Read one value from a JSON object

`Body(embed=True)` reads the value at the key of the argument name:

```python
from django_bolt.params import Body


@api.post("/articles/{article_id}/rating")
async def rate_article(article_id: int, stars: Annotated[int, Body(embed=True)]) -> dict:
    return {"article_id": article_id, "stars": stars}
```

This route reads `{"stars": 4}`. Use `Body(embed=True, alias="star-count")` to read another key.

### Read the raw body

`request.body` is the body as `bytes`:

```python
from django_bolt import Request


@api.post("/webhooks/mailer")
async def mailer_webhook(request: Request) -> dict:
    return {"size": len(request.body)}
```

## Read headers

Use `Header()`. Without an `alias`, Bolt reads the header with the argument name in lower case, with `-` for each `_`:

```python
@api.get("/articles/export")
async def export_articles(
    x_api_key: Annotated[str, Header()],
    x_page_size: Annotated[int, Header()] = 50,
) -> dict:
    return {"page_size": x_page_size}
```

This route reads `X-Api-Key` and `X-Page-Size`. Header names are not case-sensitive.
A typed header converts as a query parameter does:

```bash
curl localhost:8000/articles/export -H "X-Api-Key: secret" -H "X-Page-Size: abc"
```

```json
{"detail": "Header 'x-page-size': Invalid integer 'abc': expected a whole number such as 42 or -7"}
```

To read all the headers, use `request.headers`. It is a `dict` with lower-case names.

A header that the request sends more than once gives its last line. Bolt does not split a value at a comma.
Thus a sequence type, such as `list[str]`, gives a list of one item for a header, a cookie or a path parameter.
The OpenAPI schema of such a parameter has `maxItems: 1`.
A default of two or more items raises `TypeError` when the route registers. This rule also applies to a sequence field of a header or cookie model.

## Read cookies

Use `Cookie()`. Without an `alias`, Bolt reads the cookie with the argument name:

```python
from django_bolt.params import Cookie


@api.get("/preferences")
async def preferences(theme: Annotated[str, Cookie()] = "light") -> dict:
    return {"theme": theme}
```

To read all the cookies, use `request.cookies`.

## Read form data

Use `Form()`. Bolt reads URL-encoded bodies and multipart bodies:

```python
from django_bolt.params import Form


@api.post("/login")
async def login(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    remember: Annotated[bool, Form()] = False,
) -> dict:
    return {"username": username, "remember": remember}
```

```bash
curl -X POST localhost:8000/login -d "username=ana&password=secret&remember=true"
```

```json
{"username": "ana", "remember": true}
```

A repeated field takes its last value. Declare a `list` to take each value.

## Upload files

Use `UploadFile` with `File()`:

```python
from django_bolt import FileSize, UploadFile
from django_bolt.params import File


@api.post("/articles/{article_id}/cover")
async def upload_cover(
    article_id: int,
    cover: Annotated[UploadFile, File(max_size=FileSize.MB_5, allowed_types=["image/*"])],
) -> dict:
    data = await cover.read()
    return {"filename": cover.filename, "content_type": cover.content_type, "size": len(data)}
```

```bash
curl -X POST localhost:8000/articles/1/cover -F "cover=@cover.png;type=image/png"
```

```json
{"filename": "cover.png", "content_type": "image/png", "size": 8}
```

`File()` checks each file before the handler runs:

| Argument | Check | Error `type` |
| --- | --- | --- |
| (required argument) | The file is present | `file_missing` |
| `max_size` | Size in bytes is not above the limit | `file_too_large` |
| `min_size` | Size in bytes is not below the limit | `file_too_small` |
| `allowed_types` | The content type matches one item. `"image/*"` matches each image type | `file_invalid_content_type` |
| `max_files` | A `list[UploadFile]` has not more files than the limit | `file_too_many` |

```json
{"detail": [{"ctx": {"actual_type": "text/plain", "allowed_types": ["image/*"]}, "loc": ["body", "cover"], "msg": "Invalid content type 'text/plain'", "type": "file_invalid_content_type"}]}
```

The `BOLT_MAX_UPLOAD_SIZE` setting limits the whole request body, before `File()` runs. The default is 1 MiB. A larger body gets 413.
Set it above your largest upload, because the multipart boundaries and headers add some bytes. See the [settings reference](../ref/settings.md).

### Use an upload in the handler

`UploadFile` has these members:

- `filename`, `content_type`, `size` and `headers` describe the file.
- `await read(size=-1)`, `await seek(offset)`, `await write(data)` and `await close()` work on the content.
- `file` is a Django `File`. Assign it to a `FileField` or an `ImageField`.

```python
article.cover = cover.file
await article.asave()
```

Bolt keeps a small file in memory and writes a large file to a temporary file.
Bolt closes the files after the response.

### Take more than one file, or only the bytes

```python
@api.post("/articles/{article_id}/attachments")
async def add_attachments(article_id: int, files: Annotated[list[UploadFile], File(max_files=3)]) -> dict:
    return {"names": [f.filename for f in files]}


@api.post("/checksum")
async def checksum(data: Annotated[bytes, File(max_size=FileSize.MB_1)]) -> dict:
    return {"size": len(data)}
```

A `bytes` argument holds the full content in memory. Set `max_size` for it.

## Group parameters in a model

Give a `Query()`, `Header()`, `Cookie()` or `Form()` marker a `msgspec.Struct` type. Each field of the struct is one parameter:

```python
class ArticleFilters(msgspec.Struct):
    page: int = 1
    page_size: int = 20
    search: str | None = None
    tag: list[str] = []


@api.get("/articles/filtered")
async def filtered_articles(filters: Annotated[ArticleFilters, Query()]) -> dict:
    return {"page": filters.page, "search": filters.search, "tag": filters.tag}
```

```bash
curl "localhost:8000/articles/filtered?page=2&search=bolt&tag=a&tag=b"
```

```json
{"page": 2, "search": "bolt", "tag": ["a", "b"]}
```

- A header model reads each field name in lower case, with `-` for each `_`. The field `x_api_key` reads `X-Api-Key`.
- A form model can have `UploadFile` fields.
- A model ignores keys that it does not declare. Use `msgspec.Struct` with `forbid_unknown_fields=True` to reject them.
- With a default of `None`, the model is `None` when the request has none of its keys.

## Use the Request object

Name an argument `request` or `req` to get the request. The `Request` type hint is optional. It gives your editor the attribute names:

```python
from django_bolt import Request


@api.get("/debug/request")
async def show_request(request: Request) -> dict:
    return {
        "method": request.method,
        "path": request.path,
        "query": request.query,
        "url": request.build_absolute_uri(),
    }
```

```bash
curl "localhost:8000/debug/request?page=2"
```

```json
{"method": "GET", "path": "/debug/request", "query": {"page": "2"}, "url": "http://localhost:8000/debug/request?page=2"}
```

An argument with another name, such as `r: Request`, is a query parameter, and Bolt raises `TypeError`.

These are the most used attributes. The [Request reference](../ref/request.md) lists each one.

| Attribute | Value |
| --- | --- |
| `method`, `path` | The HTTP method and the path, as `str` |
| `body` | The raw body, as `bytes` |
| `headers`, `cookies`, `query` | `dict` of the headers, cookies and query values |
| `params` | `dict` of the path parameters, converted to their types |
| `state` | A `dict` for your own data during the request, for example from middleware |
| `user` | The user that an authentication backend found, or Django's `AnonymousUser`. Without `django.contrib.auth` in `INSTALLED_APPS`, it is `None` |
| `context` | The authentication data, for example the JWT claims, or `None` |

### Get the current user

On a route with an authentication backend, `await request.auser()` returns the user. It returns `AnonymousUser` when no user is authenticated.

```python
from django_bolt import IsAuthenticated, JWTAuthentication


@api.get("/me", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def me(request: Request) -> dict:
    user = await request.auser()
    return {"username": user.username}
```

`request.user` also works. In an async handler, it loads the user with a blocking database query. Use `await request.auser()` there.
See [Authentication](authentication.md).

### Build absolute URLs

`request.build_absolute_uri(location=None)`, `request.scheme` and `request.is_secure()` work as in Django.
`build_absolute_uri()` checks the host against `ALLOWED_HOSTS`. A host that is not allowed gets a 400 response.

Bolt does not end TLS, so the scheme is `"https"` only when a trusted proxy says so.
Set the proxies in [`BOLT_TRUSTED_PROXIES`](../ref/settings.md). Django's `SECURE_PROXY_SSL_HEADER` also applies.

## Validation errors

A value that fails conversion or validation gets a 422 response. The handler does not run.
The body has one of two forms.

A value that Bolt converts before Python runs gives a `detail` string. This covers a path, query, header, cookie or form value with the wrong type, and a missing value of these kinds:

```json
{"detail": "Query parameter 'page': Invalid integer 'x': expected a whole number such as 42 or -7"}
```

```json
{"detail": "Missing required query parameter: query"}
```

Each other error gives a `detail` list. This covers the JSON body, limits, parameter models and files:

```json
{"detail": [{"loc": ["body", "title"], "msg": "Object missing required field `title`", "type": "missing_field"}]}
```

Each item has `loc`, `msg` and `type`. Some items also have `input` and `ctx`.
`loc` starts with the source: `"path"`, `"query"`, `"header"`, `"cookie"` or `"body"`.

Use an error formatter to give each error the same form. See [Error handling](error-handling.md#change-the-error-format).

## See also

- [Parameters reference](../ref/params.md)
- [Request reference](../ref/request.md)
- [Responses](responses.md)
- [Error handling](error-handling.md)
- [Dependencies](dependencies.md)
- [Serializers](serializers.md)
