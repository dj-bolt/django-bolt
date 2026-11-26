---
description: Map methods and paths to handlers, read path parameters, group routes, mount an API in the Django URLconf, and control trailing slashes.
---

# Routing

A route is an HTTP method and a path. A handler is the function that serves the route.
You register a route with a decorator on a `BoltAPI` instance or on a `Router`.

```python title="blog/api.py"
from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/articles")
async def list_articles() -> list[dict]:
    return [{"id": 1, "title": "Hello"}]


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    return {"id": article_id, "title": "Hello"}


@api.post("/articles", status_code=201)
async def create_article() -> dict:
    return {"id": 2, "title": "New"}
```

```bash
curl localhost:8000/articles/3
```

```json
{"id": 3, "title": "Hello"}
```

`runbolt` finds this `api` object when it starts. See [Find the API modules](#find-the-api-modules).

## Use an HTTP method

Bolt has one decorator for each method:

| Decorator | Method |
| --- | --- |
| `@api.get(path)` | `GET` |
| `@api.post(path)` | `POST` |
| `@api.put(path)` | `PUT` |
| `@api.patch(path)` | `PATCH` |
| `@api.delete(path)` | `DELETE` |
| `@api.head(path)` | `HEAD` |
| `@api.options(path)` | `OPTIONS` |
| `@api.query(path)` | `QUERY` |

A request to a known path with a method that has no route gets `405 Method Not Allowed`.
The `Allow` header lists the methods of the path.
A request to an unknown path gets `404 Not Found`.

```bash
curl -i -X DELETE localhost:8000/articles
```

```http
HTTP/1.1 405 Method Not Allowed
allow: GET, POST, OPTIONS
content-type: application/json

{"detail":"Method Not Allowed"}
```

Bolt answers `OPTIONS` for each path. The response is `204 No Content` with the `Allow` header.
Register `@api.options()` to send your own response.

Bolt does not add a `HEAD` route for a `GET` route. A `HEAD` request to a `GET` route gets 405.
Register `@api.head()` to serve `HEAD`. Bolt sends the status and headers of the handler, with no body.

### Serve more than one method with one handler

Put more than one decorator on the handler:

```python
@api.get("/drafts")
@api.post("/drafts")
async def drafts() -> dict:
    return {"drafts": []}
```

Each decorator registers its own route.

### Send a body with QUERY

`QUERY` is a safe method that has a request body, as `GET` would if it had one.
Use it for a search that is too large or too structured for a query string.

```python
import msgspec


class ArticleSearch(msgspec.Struct):
    tags: list[str]
    limit: int = 20


@api.query("/articles/search")
async def search_articles(search: ArticleSearch) -> dict:
    return {"tags": search.tags, "limit": search.limit}
```

```bash
curl -X QUERY localhost:8000/articles/search \
  -H "Content-Type: application/json" -d '{"tags": ["django"]}'
```

```json
{"tags": ["django"], "limit": 20}
```

The OpenAPI 3.1 document that Bolt makes has no `query` operation. A strict OpenAPI 3.1 validator rejects such a route.

## Read path parameters

Put the parameter name in braces. Give the handler an argument with the same name.
The type hint of the argument sets the type:

```python
@api.get("/authors/{author_id}/articles/{slug}")
async def get_author_article(author_id: int, slug: str) -> dict:
    return {"author_id": author_id, "slug": slug}
```

A value that does not convert to the type gets a 422 response:

```bash
curl localhost:8000/authors/abc/articles/hello
```

```json
{"detail": "Path parameter 'author_id': Invalid integer 'abc': invalid digit found in string"}
```

The type comes only from the type hint. Bolt ignores a type in the path, such as `{author_id:int}`.
Give each parameter of a path its own name. A path such as `/d/{x}/{x}` raises `ValueError` when you register it.

[Requests](requests.md#read-path-parameters) shows the types that Bolt converts and how to add limits.

### Match the remainder of a path

Write `{name:path}` to match all the remaining segments, slashes included:

```python
@api.get("/files/{file_path:path}")
async def get_file(file_path: str) -> dict:
    return {"file_path": file_path}
```

```bash
curl localhost:8000/files/2026/01/report.pdf
```

```json
{"file_path": "2026/01/report.pdf"}
```

### Route order

The order in which you register routes has no effect.
A fixed segment wins over a parameter in the same position:

```python
@api.get("/authors/{username}")
async def get_author(username: str) -> dict:
    return {"username": username}


@api.get("/authors/me")
async def get_current_author() -> dict:
    return {"username": "me"}
```

`GET /authors/me` calls `get_current_author`. `GET /authors/ana` calls `get_author`.

### Routes that stop the startup

The router refuses a route that can never get a request.
The test clients then raise `ValueError`, and `runbolt` stops with `CommandError`.
The message gives the method and the path of the route.
For a WebSocket route, the method is `WEBSOCKET`.
The path is the full path, with the prefix, the mount path and the trailing slash mode applied.

- **The same method and path.** Two `GET /items/{id}` routes give `GET /items/{id} is already registered`.
  With `trailing_slash="strip"`, `/tags` and `/tags/` are the same path.
  A WebSocket path has no trailing slash, so `/live` and `/live/` are the same WebSocket path.
  Remove one of the routes.
- **Two parameters at one position.** `/files/{file_id}` and `/files/{file_path:path}` give `GET /files/{file_path:path} conflicts with GET /files/{file_id}`.
  The same applies to `/articles/{slug}` and `/articles/{article_id}`, and to two `{name:path}` parameters.
  The router cannot select one of the two for `/files/abc`.
  Keep one route and read the value in its handler.
  Or add a fixed segment before the parameter in one path, for example `/files/by-path/{file_path:path}`.
- **A `{name:path}` parameter before the end.** `/files/{p:path}/meta` gives `has a {name:path} parameter before the end of the path`.
  Move the parameter to the end of the path.
- **Two parameters in one segment.** `/files/{name}-{version}` gives `has two parameters in one segment`.
  Use one parameter for the segment, and split the value in the handler.
- **A parameter with no name, or a `{` with no `}`.** `/files/{}` and `/files/{id` give `has a parameter with no name, or a brace with no pair`.
  Write each parameter as `{name}` or `{name:type}`.
  A `}` with no `{` before it is a plain character, so `/files/id}` registers.

## Add a prefix to each route

Give `BoltAPI` a `prefix`:

```python
api = BoltAPI(prefix="/api/v1")


@api.get("/articles")  # serves /api/v1/articles
async def list_articles() -> list[dict]:
    return []
```

Bolt adds a leading slash to each path and each prefix. Thus `BoltAPI(prefix="api/v1")` with `@api.get("articles")` also serves `/api/v1/articles`.

## Group routes with a router

A `Router` holds a group of routes with a shared prefix, tags, middleware, authentication and guards.
Include the router in the API with `include_router()`:

```python
from django_bolt import BoltAPI, Router

comments = Router(prefix="/comments", tags=["comments"])


@comments.get("")
async def list_comments() -> list[dict]:
    return [{"id": 1, "body": "Nice post"}]


@comments.get("/{comment_id}")
async def get_comment(comment_id: int) -> dict:
    return {"id": comment_id}


api = BoltAPI()
api.include_router(comments, prefix="/v1")
```

```bash
curl localhost:8000/v1/comments/3
```

```json
{"id": 3}
```

A route on a router takes the `tags`, `auth` and `guards` of the router when the route does not set them.
The router decorators take the same keyword arguments as the `BoltAPI` decorators.
`Router` has no `websocket()`, `view()` or `viewset()`. Register these on the `BoltAPI`.

### Nest routers

Include a router in another router. The prefixes join, from the outer router to the inner router:

```python
v1 = Router(prefix="/v1")
tags = Router(prefix="/tags")


@tags.get("")
async def list_tags() -> list[str]:
    return ["django", "rust"]


v1.include_router(tags)
api.include_router(v1)  # GET /v1/tags
```

An inner router takes the `auth`, `guards` and `tags` that it does not set from the nearest outer router that sets them.
Its routes run the middleware of the outer routers first, then its own middleware.

## Mount an API in another API

`mount()` copies the routes of one `BoltAPI` into another, under a path.
The mounted API keeps its own middleware and its own `django_middleware`:

```python
admin_api = BoltAPI(django_middleware=True)


@admin_api.get("/stats")
async def stats() -> dict:
    return {"articles": 12}


api = BoltAPI()
api.mount("/admin", admin_api)  # GET /admin/stats
```

Use a `Router` to share auth and guards. Use `mount()` for a group of routes that needs a different middleware stack.

To put an API under a path of the Django URLconf, see [Mount an API in the URLconf](#mount-an-api-in-the-urlconf).

A mount path can have a parameter, for example `/tenants/{tenant}`.
A handler or a dependency of the mounted API reads its value from `request.params["tenant"]`, or from an argument with the `Path()` marker.
An argument with no marker cannot have the name `tenant`, because it reads the query string. `mount()` raises `ValueError` for such a handler or dependency.
An argument with another marker, such as `Query()`, reads the source of its marker, as on each route.

To serve a Django view or another ASGI application under a path, see [ASGI mounts](asgi-mounts.md).

## Control trailing slashes

The `trailing_slash` argument of `BoltAPI` sets the form of each path:

| Value | Effect | `@api.get("/tags/")` serves |
| --- | --- | --- |
| `"strip"` (default) | Removes the trailing slash | `/tags` |
| `"append"` | Adds a trailing slash | `/tags/` |
| `"keep"` | Keeps the path as you wrote it | `/tags/` |

A request to the other form of a path gets `308 Permanent Redirect` to the registered form.
The redirect keeps the query string and the method.

```bash
curl -i localhost:8000/tags/?page=2
```

```http
HTTP/1.1 308 Permanent Redirect
location: /tags?page=2
```

When the registered form has no route for the method, the request gets 405 with the `Allow` header of the registered form.

Another value of `trailing_slash` raises `ValueError`.
Each `BoltAPI` keeps its own mode when `runbolt` joins the APIs of a project.
A mounted API takes the mode of the API that mounts it.

A WebSocket route accepts both forms of its path, in each mode. A WebSocket client usually does not follow a redirect.

## Name a route

Give a route a name with `name=`. Django's `reverse()` and the `{% url %}` tag then find it:

```python
@api.get("/articles/{article_id}", name="article-detail")
async def get_article(article_id: int) -> dict:
    return {"id": article_id}
```

A route with no `name` gets the name of its handler function, for example `"get_article"`.
Two routes with the same explicit name and different paths raise `ImproperlyConfigured`.

Put the API in your URLconf to give Django the names. Use one of the two forms below.

## Mount an API in the URLconf

`api.urls` puts an API in Django's URLconf, as `admin.site.urls` does:

```python title="blog/urls.py"
from django.urls import path

from blog.api import api

urlpatterns = [
    path("blog/", api.urls),
]
```

Bolt reads the URLconf one time at startup. It serves each route of the API under the prefix, and `reverse()` gives the same path:

```pycon
>>> from django.urls import reverse
>>> reverse("article-detail", kwargs={"article_id": 42})
'/blog/articles/42'
```

```bash
curl localhost:8000/blog/articles/42
```

```json
{"id": 42}
```

The API does not serve `/articles/42` any more.
Bolt joins the prefix and the route path as Django does. Thus the root route `"/"` of the API serves at `/blog/`, also when `trailing_slash="strip"`.
An API that the URLconf mounts and that `runbolt` also finds serves one time, under its prefixes. An API that the URLconf mounts is served also when `runbolt` does not find it.
`TestClient(api)` serves an API that the URLconf mounts under its prefixes too. Thus it imports `ROOT_URLCONF`.
A URLconf that does not import makes `TestClient` raise. It also makes the system checks of `runbolt` fail.
Behind a proxy that removes a sub-path, `reverse()` also puts `FORCE_SCRIPT_NAME` first. See [Serve under a sub-path](deployment.md#serve-under-a-sub-path).

### Reverse through namespaces

`BoltAPI(namespace="blog")` is the app namespace and the instance namespace of `api.urls`. An enclosing `include()` adds its namespace in front, as for each Django app:

```python title="blog/urls.py"
from django.urls import path

from blog.api import api  # BoltAPI(namespace="api")

app_name = "blog"
urlpatterns = [path("api/", api.urls)]
```

```python title="mysite/urls.py"
from django.urls import include, path

urlpatterns = [path("blog/", include("blog.urls"))]
```

```pycon
>>> reverse("blog:api:article-detail", kwargs={"article_id": 42})
'/blog/api/articles/42'
```

A route that the API copied with `mount()` also gets the namespace of the mounted API: `"blog:api:<namespace>:<name>"`.
Each level of `mount()` adds the namespace of its API. Thus a mounted API can have the same namespace as its parent, for example `"blog:api:api:<name>"`.
A mounted API with no namespace adds no level, so its names join the names of its parent.

### Use a parameter in the prefix

A `str` parameter in the prefix becomes a path parameter, for example `path("<tenant>/", api.urls)`.
A handler or a dependency reads its value as for a [mount path](#mount-an-api-in-another-api): from `request.params["tenant"]`, or from an argument with `Path()`.
`reverse()` needs it as a keyword argument.
The docs of an API cannot be under such a prefix. Set `OpenAPIConfig(enabled=False)` on the API.
An ASGI mount, a Django mount and an MCP server cannot be under such a prefix either. See [What moves with the prefix](#what-moves-with-the-prefix).

Bolt serves an API at static paths. `runbolt` and the test clients refuse these prefixes with `ImproperlyConfigured`, and the message names the URLconf entry:

- a `re_path()` prefix, an `i18n_patterns()` prefix, or a translated prefix
- a converter other than `str`, for example `<int:org>`, because the Bolt router does not check it
- two parameters in one path segment, for example `<a>-<b>/`, because the Bolt router allows one
- one parameter name two times, for example `<org>/<org>/`, or a prefix parameter with the name of a route parameter
- a prefix that does not end with a slash, or a prefix that gives an empty path segment
- keyword arguments in the include, because a Bolt handler does not get them

Django alone does not serve Bolt routes, so it does not refuse these prefixes.
`manage.py check`, `manage.py migrate`, `reverse()` and the Django views work with such a URLconf.
There is one exception. When one parameter name is two times in a path, `reverse()` raises the error of Django itself.

### What moves with the prefix

Each kind of route of the API moves under the prefix of `path("blog/", api.urls)`.
The last column tells if the kind works under a prefix with a parameter, such as `path("<tenant>/", api.urls)`:

| Kind | Path without a mount | Path with the mount | Under a prefix parameter |
| --- | --- | --- | --- |
| HTTP route | `/articles/{id}` | `/blog/articles/{id}` | Yes |
| WebSocket route | `/live` | `/blog/live` | Yes |
| Native route | `/items/{id}` | `/blog/items/{id}` | Yes |
| `mount_asgi()`, `mount_django()` | `/files` | `/blog/files` | No, `runbolt` refuses the prefix |
| `mount_mcp()` | `/mcp` | `/blog/mcp` | No, `runbolt` refuses the prefix |
| Docs (`OpenAPIConfig.path`) | `/docs` | `/blog/docs` | No, set `OpenAPIConfig(enabled=False)` |

The paths in the OpenAPI schema are the served paths.
When `runbolt` joins several APIs, the docs of the first API list each route. They go under the first prefix of that API.
An MCP server with `oauth=` cannot move, because its OAuth audience names its path. Mount its API with `path("", api.urls)`.
The Django admin and the static files are not part of an API, so they do not move.

### Reverse an API that the URLconf does not mount

Include `django_bolt.urls` to name the routes of each API that `runbolt` finds and that no `api.urls` mounts:

```python title="mysite/urls.py"
from django.urls import include, path

urlpatterns = [
    path("", include("django_bolt.urls")),
]
```

```pycon
>>> reverse("article-detail", kwargs={"article_id": 42})
'/articles/42'
```

`BoltAPI(namespace="blog")` puts each route of the API in a namespace. Then use `reverse("blog:article-detail", ...)`.

A prefix on this include moves these APIs, as `api.urls` does. With `path("api/", include("django_bolt.urls"))`, Bolt serves `/api/articles/42`, and `reverse()` gives the same path.
The prefix rules of `api.urls` apply, but Bolt ignores the keyword arguments of the include.
`TestClient(api)` serves such an API under the prefix too.
Under a prefix that Bolt cannot serve, such as `re_path(r"^api/", ...)`, Django alone gives the same names and paths as before. Only `runbolt` and the test clients refuse it.
When `ROOT_URLCONF` changes, for example with `override_settings` in a test, `django_bolt.urls` reads the new URLconf.

`django_bolt.urls.urlpatterns` is a list with one include. Thus a URLconf can copy it with `[*urlpatterns]` or join it to a list with `+`.

## Describe a route in the OpenAPI document

The decorators take arguments for the [OpenAPI document](openapi.md):

```python
@api.get(
    "/articles/{article_id}",
    tags=["articles"],
    summary="Get an article",
    description="Return one published article.",
    deprecated=True,
)
async def get_article(article_id: int) -> dict:
    return {"id": article_id}
```

- `tags` groups the operation in the documentation UI.
- `summary` and `description` set the text of the operation. Without them, the first line of the handler docstring is the summary, and the remaining lines are the description.
- `deprecated=True` marks the operation as deprecated. The route still works.
- `include_in_schema=False` removes the route from the document. The route still works.

## Use a sync handler

A handler can be a `def` function:

```python
from django.contrib.auth.models import User


@api.get("/authors/count")
def count_authors() -> dict:
    return {"count": User.objects.count()}
```

Bolt reads the source of a sync handler when you register it.
A sync handler that uses the ORM or blocking I/O runs in a thread pool.
Each other sync handler runs on the worker thread.
See [Async and the ORM](async-orm.md).

A handler is a function, a bound method, or an object with a `__call__` method.
A `functools.partial` raises `TypeError`. Register a function that calls the partial.

## Find the API modules

`runbolt` imports your API objects when it starts. It looks in these places, in this order:

1. The `BOLT_API` setting, if you set it. It is a list of `"module:attribute"` strings, for example `["blog.api:api"]`. Bolt then looks nowhere else.
2. The `api.py` or `bolt_api.py` module of the project package. The project package is the package of `ROOT_URLCONF`.
3. The `api.py` or `bolt_api.py` module of each installed app. An app can name another module with a `bolt_api = "module:attribute"` attribute on its `AppConfig`.

In each module, Bolt takes the first top-level assignment of a `BoltAPI()` call, for example `api = BoltAPI()`.
It reads the source of the module to find it, so `api = make_api()` is not found.
Bolt joins the routes of each API that it finds into one server.
An API that another API mounts must come after the main API in the same module.
An API that the URLconf mounts with `api.urls` serves under its prefixes. See [Mount an API in the URLconf](#mount-an-api-in-the-urlconf).

[Installation](../intro/install.md) shows the minimum project layout.

## See also

- [BoltAPI and Router reference](../ref/api.md)
- [Requests](requests.md)
- [Responses](responses.md)
- [Class-based views](class-based-views.md)
- [WebSocket](websocket.md)
- [ASGI mounts](asgi-mounts.md)
