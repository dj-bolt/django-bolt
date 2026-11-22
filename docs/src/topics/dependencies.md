---
description: Share request logic between handlers with Depends(), with sync and async dependencies, nested dependencies, per-request caching and the current user.
---

# Dependencies

A dependency is a function that Bolt calls before the handler. Its return value goes to a handler parameter. Use a dependency for logic that several handlers share, for example "load the article or answer 404".

## Add a dependency

Give a handler parameter the default `Depends(function)`:

```python title="blog/api.py"
import msgspec

from django_bolt import BoltAPI, Depends

from blog.models import Article

api = BoltAPI()


class PageParams(msgspec.Struct):
    offset: int
    limit: int


def page_params(page: int = 1, size: int = 10) -> PageParams:
    page = max(page, 1)
    size = min(max(size, 1), 50)
    return PageParams(offset=(page - 1) * size, limit=size)


@api.get("/articles")
async def list_articles(paging: PageParams = Depends(page_params)) -> dict:
    rows = Article.objects.order_by("id")[paging.offset : paging.offset + paging.limit]
    return {"offset": paging.offset, "titles": [article.title async for article in rows]}
```

```bash
curl "localhost:8000/articles?page=2&size=1"
```

```json
{"offset": 1, "titles": ["Async ORM"]}
```

Bolt reads the parameters of the dependency from the request, as for a handler. Here `page` and `size` are query parameters. A dependency can take path, query, header, cookie, body, form and file parameters, the request, and other dependencies. The OpenAPI schema of the route lists the request parameters of its dependencies.

You can also put the marker in `Annotated`. An alias then makes the dependency short to reuse:

<!-- fragment -->
```python
from typing import Annotated

Paging = Annotated[PageParams, Depends(page_params)]


@api.get("/comments")
async def list_comments(paging: Paging) -> dict: ...
```

## Raise an error from a dependency

A dependency can stop the request. Raise an `HTTPException`, or one of its subclasses such as `NotFound` or `Forbidden`. The handler does not run:

```python
from django_bolt import BoltAPI, CurrentUser, Depends
from django_bolt.auth import JWTAuthentication
from django_bolt.exceptions import Forbidden, NotFound

from blog.models import Article

api = BoltAPI()


async def get_article(article_id: int) -> Article:
    try:
        return await Article.objects.aget(id=article_id)
    except Article.DoesNotExist:
        raise NotFound(detail="Article not found")


async def get_own_article(user: CurrentUser, article: Article = Depends(get_article)) -> Article:
    if article.author_id != user.id:
        raise Forbidden(detail="You are not the author")
    return article


@api.delete("/articles/{article_id}", auth=[JWTAuthentication()])
async def delete_article(article: Article = Depends(get_own_article)) -> dict:
    await article.adelete()
    return {"deleted": True}
```

| Request | Response |
| --- | --- |
| No token | `401 {"detail": "Authentication required"}` |
| A token of another user | `403 {"detail": "You are not the author"}` |
| An article that does not exist | `404 {"detail": "Article not found"}` |
| A token of the author | `200 {"deleted": true}` |

`get_article` reads `article_id` from the path of the route. `get_own_article` depends on `get_article` and on the current user. Bolt resolves the nested dependencies first.

## Get the current user

`CurrentUser` and `OptionalCurrentUser` are ready dependencies for the user of the request:

| Type | Without an authenticated user |
| --- | --- |
| `CurrentUser` | `401` response |
| `OptionalCurrentUser` | `None` |

Both load the user and do not block the event loop. A sync handler gets a sync form of the same dependency, which reads `request.user` on the thread of the handler. For your own user model type, make an alias: `Annotated[User, Depends(get_current_user)]`, with `get_current_user` from `django_bolt.auth`.

A guard such as `IsAuthenticated()` runs in Rust before the dependencies. Use a guard for a check of a token claim. Use a dependency for a check that needs the database, as `get_own_article` above. See [Permissions](permissions.md).

## Cache a dependency for one request

Bolt calls each dependency one time for each request, and gives the same value to each parameter that needs it:

```python
from typing import Annotated

from django_bolt import BoltAPI, Depends
from django_bolt.param_functions import Header

api = BoltAPI()


def get_api_version(x_api_version: Annotated[str, Header()] = "1") -> str:
    return x_api_version


def get_label(version: str = Depends(get_api_version)) -> str:
    return f"v{version}"


@api.get("/version")
async def version(label: str = Depends(get_label), version: str = Depends(get_api_version)) -> dict:
    return {"label": label, "version": version}
```

```bash
curl localhost:8000/version -H "X-API-Version: 2"
```

```json
{"label": "v2", "version": "2"}
```

`get_api_version` runs one time, although two parameters need it. The cache key is the dependency function. The cache lives for one request.

`Depends(function, use_cache=False)` calls the function again for each parameter.

## Use sync and async dependencies

A dependency can be `def` or `async def`. Bolt awaits an async dependency.

- When a handler has two or more async dependencies, Bolt runs them at the same time. A cached dependency that two of them need runs first, so it still runs one time.
- When each dependency of a handler is sync, Bolt resolves them with no event loop. A sync handler then keeps its fast sync dispatch.
- A sync handler that runs on a thread pool resolves its sync dependencies on the same thread. They can use the sync ORM. Behind Django middleware, they see the thread-local state of the request.

## Use a class as a dependency

An object with a `__call__` method is a dependency. Bolt reads the parameters of `__call__`. Use this for a dependency with settings:

```python
from django_bolt import BoltAPI, Depends

api = BoltAPI()


class MaxSize:
    def __init__(self, maximum: int):
        self.maximum = maximum

    def __call__(self, size: int = 20) -> int:
        return min(size, self.maximum)


@api.get("/sizes")
async def sizes(size: int = Depends(MaxSize(100))) -> dict:
    return {"size": size}
```

```bash
curl "localhost:8000/sizes?size=500"
```

```json
{"size": 100}
```

Bolt awaits the call when `__call__` is `async def`.

## Read the request in a dependency

A parameter named `request` gets the request object:

```python
from django_bolt import BoltAPI, Depends, Request

api = BoltAPI()


def client_ip(request: Request) -> str:
    # Bolt reads X-Forwarded-For only from a proxy in BOLT_TRUSTED_PROXIES.
    return request.META["REMOTE_ADDR"]


@api.get("/ip")
async def show_ip(ip: str = Depends(client_ip)) -> dict:
    return {"ip": ip}
```

With `BOLT_TRUSTED_PROXIES = ["127.0.0.1"]`, a request from the proxy at `127.0.0.1` gives the address that the proxy forwards:

```bash
curl localhost:8000/ip -H "X-Forwarded-For: 203.0.113.7"
```

```json
{"ip": "203.0.113.7"}
```

Do not read `X-Forwarded-For` yourself. A client can send this header with any address. See [`BOLT_TRUSTED_PROXIES`](../ref/settings.md#bolt_trusted_proxies).

## Limits

- A dependency returns a value. Bolt has no cleanup dependencies. A generator function, with `yield`, raises `TypeError` at registration. Do the cleanup in the handler, or in [middleware](middleware.md).
- A dependency or a handler cannot have `*args` or `**kwargs`. Bolt raises `TypeError` at registration.
- `Router` has no `dependencies` argument. Add the dependency to each handler, or use an `Annotated` alias.
- Bolt has no API that replaces a dependency in a test. Make the dependency read data that the test controls, such as a header, a row or a setting. See [Testing](testing.md).

## Use dependencies in class-based views

The methods of an `APIView` or a `ViewSet` take dependencies as a function handler does:

```python
import msgspec

from django_bolt import BoltAPI, Depends, ViewSet

from blog.models import Article

api = BoltAPI()


class PageParams(msgspec.Struct):
    offset: int
    limit: int


def page_params(page: int = 1, size: int = 10) -> PageParams:
    page = max(page, 1)
    size = min(max(size, 1), 50)
    return PageParams(offset=(page - 1) * size, limit=size)


@api.viewset("/articles")
class ArticleViewSet(ViewSet):
    queryset = Article.objects.order_by("id")

    async def list(self, paging: PageParams = Depends(page_params)) -> list[str]:
        queryset = await self.get_queryset()
        return [article.title async for article in queryset[paging.offset : paging.offset + paging.limit]]
```

```bash
curl "localhost:8000/articles?size=2"
```

```json
["Hello Bolt", "Async ORM"]
```

## See also

- [Requests](requests.md)
- [Authentication](authentication.md)
- [Permissions](permissions.md)
- [Class-based views](class-based-views.md)
- [Testing](testing.md)
- [Parameters reference](../ref/params.md)
