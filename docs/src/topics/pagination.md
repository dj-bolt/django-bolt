---
description: Split a QuerySet or a list into pages with page number, limit-offset or cursor pagination, in function routes and ViewSets.
---

# Pagination

The `@paginate` decorator splits the result of a handler into pages. The handler returns a QuerySet or a list. Bolt reads the page from the query string, runs the queries, and returns one page in an envelope with page information.

## Paginate a list route

Put `@paginate(...)` under the route decorator. Give the handler the return type `list[Item]`, where `Item` is the type of one row:

```python title="blog/api.py"
import msgspec

from django_bolt import BoltAPI, PageNumberPagination, paginate

from blog.models import Article

api = BoltAPI()


class ArticleItem(msgspec.Struct):
    id: int
    title: str


class ArticlePages(PageNumberPagination):
    page_size = 2


@api.get("/articles")
@paginate(ArticlePages)
async def list_articles() -> list[ArticleItem]:
    return Article.objects.filter(published=True).order_by("id")
```

```bash
curl localhost:8000/articles
```

```json
{"items": [{"id": 1, "title": "Hello Bolt"}, {"id": 2, "title": "Async ORM"}], "total": 24, "has_next": true, "has_previous": false, "page": 1, "page_size": 2, "total_pages": 12, "next_page": 2}
```

```bash
curl "localhost:8000/articles?page=3&page_size=3"
```

```json
{"items": [{"id": 8, "title": "Article 8"}, {"id": 9, "title": "Article 9"}, {"id": 10, "title": "Article 10"}], "total": 24, "has_next": true, "has_previous": true, "page": 3, "page_size": 3, "total_pages": 8, "next_page": 4, "previous_page": 2}
```

Order the QuerySet. A QuerySet with no order can give a row on two pages, or on none.

The response type of the route is the envelope, `PaginatedResponse[ArticleItem]`. The OpenAPI schema shows the envelope and the query parameters of the class. The schema of the page size has the `page_size` default and the `max_page_size` maximum.

The envelope leaves out each field that is `None`. Thus the last page has no `next_page`, and the first page has no `previous_page`.

Bolt converts each item to the item type, as for a route with no `@paginate`. Declare the item type. With no return type, each item is a dict of all the columns of the model. A nested Struct of a foreign key or a one-to-one relation loads in the query of the page, with `select_related()`.

## Choose a pagination class

| Class | Query parameters | Envelope fields | Count query | Use for |
| --- | --- | --- | --- | --- |
| `PageNumberPagination` (default) | `page`, `page_size` | `page`, `page_size`, `total_pages`, `next_page`, `previous_page` | Yes | Pages with numbers |
| `LimitOffsetPagination` | `limit`, `offset` | `limit`, `offset` | Yes | Clients that skip a number of rows |
| `CursorPagination` | `cursor`, `page_size` | `page_size`, `next_cursor` | No | Large or fast-changing tables, infinite scroll |

Each envelope also has `items`, `total`, `has_next` and `has_previous`.

`@paginate()` with no argument uses `PageNumberPagination`.

## Set the page size

Make a subclass and set its class attributes:

| Attribute | Default | Effect |
| --- | --- | --- |
| `page_size` | `100` | The rows of a page when the request does not set a size. For `LimitOffsetPagination`, the default `limit`. |
| `max_page_size` | `1000` | The largest page. It also limits `page_size`. |
| `page_size_query_param` | `"page_size"` | The query parameter for the size. `None` turns it off. `LimitOffsetPagination` reads `limit` instead. |

Bolt corrects a bad value and does not answer `400`:

- A `page` that is not an integer, or is less than 1, is page 1.
- A `page` after the last page is the last page.
- A size that is not an integer, or is less than 1, is `page_size`.
- A size more than `max_page_size` is `max_page_size`.
- A bad `offset` is 0. A bad `limit` is `page_size`.

## Paginate by limit and offset

```python
import msgspec

from django_bolt import BoltAPI, LimitOffsetPagination, paginate

from blog.models import Article

api = BoltAPI()


class ArticleItem(msgspec.Struct):
    id: int
    title: str


@api.get("/articles")
@paginate(LimitOffsetPagination)
async def list_articles() -> list[ArticleItem]:
    return Article.objects.order_by("id")
```

```bash
curl "localhost:8000/articles?limit=2&offset=4"
```

```json
{"items": [{"id": 5, "title": "Article 5"}, {"id": 6, "title": "Article 6"}], "total": 25, "has_next": true, "has_previous": true, "limit": 2, "offset": 4}
```

An offset at or after the end gives an empty page and sends no row query.

## Paginate with a cursor

A cursor page continues after the last row of the previous page. It sends no `COUNT` query and does not skip rows with `OFFSET`. Set the ordering on the class:

```python
import msgspec

from django_bolt import BoltAPI, CursorPagination, paginate

from blog.models import Article

api = BoltAPI()


class ArticleItem(msgspec.Struct):
    id: int
    title: str


class ArticleFeed(CursorPagination):
    page_size = 2
    ordering = "-created_at"


@api.get("/feed")
@paginate(ArticleFeed)
async def feed() -> list[ArticleItem]:
    return Article.objects.filter(published=True)
```

```bash
curl localhost:8000/feed
```

```json
{"items": [{"id": 25, "title": "Article 25"}, {"id": 24, "title": "Article 24"}], "total": 0, "has_next": true, "has_previous": false, "page_size": 2, "next_cursor": "eyJ2IjoiMjAyNi0xMS0yMlQwMjowOToyNi44MzgzMTRaIiwicGsiOjI0fQ=="}
```

Send `next_cursor` back as `cursor` to get the next page:

```bash
curl "localhost:8000/feed?cursor=eyJ2IjoiMjAyNi0xMS0yMlQwMjowOToyNi44MzgzMTRaIiwicGsiOjI0fQ=="
```

```json
{"items": [{"id": 23, "title": "Article 23"}, {"id": 22, "title": "Article 22"}], "total": 0, "has_next": true, "has_previous": true, "page_size": 2, "next_cursor": "eyJ2IjoiMjAyNi0xMS0yMlQwMjowOToyNi44MzU4MjRaIiwicGsiOjIyfQ=="}
```

The rules of `CursorPagination`:

- `ordering` is one field name, with `-` for descending order. The default is `"-id"`.
- Bolt orders by the field and then by the primary key. Thus the field does not have to be unique.
- The cursor uses the URL-safe base64 alphabet. A client can put it in a query string as it is.
- `total` is always `0`. `has_previous` is `true` when the request has a valid cursor. The envelope has no cursor to go back.
- A cursor that does not decode, or that does not fit the field, gives the first page.
- The handler must return a QuerySet. A list raises `TypeError`.
- The field must not be null. A null value in the last row of a page raises `ValueError`.
- A `values()` QuerySet must include the ordering field and the primary key.
- For a `ForeignKey`, the cursor holds the key. Order by the key name, such as `"author_id"`, when the related model has `Meta.ordering`.

## Paginate a list

`PageNumberPagination` and `LimitOffsetPagination` also take a list or a tuple from the handler:

```python
import msgspec

from django_bolt import BoltAPI, PageNumberPagination, paginate

api = BoltAPI()


class ArticleItem(msgspec.Struct):
    id: int
    title: str


class ArticlePages(PageNumberPagination):
    page_size = 2


@api.get("/featured")
@paginate(ArticlePages)
async def featured() -> list[ArticleItem]:
    return [ArticleItem(id=1, title="Hello Bolt"), ArticleItem(id=2, title="Async ORM"), ArticleItem(id=7, title="Lanes")]
```

```bash
curl "localhost:8000/featured?page=2"
```

```json
{"items": [{"id": 7, "title": "Lanes"}], "total": 3, "has_next": false, "has_previous": true, "page": 2, "page_size": 2, "total_pages": 2, "previous_page": 1}
```

## Use a Serializer as the item type

With a Bolt `Serializer` item type, Bolt applies the loading plan of the serializer to the page query. Nested relations then cost a fixed number of queries for each page:

```python
from django_bolt import BoltAPI, PageNumberPagination, paginate
from django_bolt.serializers import Serializer

from blog.models import Article

api = BoltAPI()


class AuthorOut(Serializer):
    username: str


class ArticleOut(Serializer):
    id: int
    title: str
    author: AuthorOut


@api.get("/articles")
@paginate(PageNumberPagination)
async def list_articles() -> list[ArticleOut]:
    return Article.objects.order_by("id")
```

```json
{"items": [{"id": 1, "title": "Hello Bolt", "author": {"username": "alice"}}, {"id": 2, "title": "Async ORM", "author": {"username": "alice"}}, {"id": 3, "title": "Draft", "author": {"username": "bob"}}], "total": 3, "has_next": false, "has_previous": false, "page": 1, "page_size": 100, "total_pages": 1}
```

See [Serializers](serializers.md#return-a-queryset) for the loading plan.

## Filter before you paginate

A paginated handler takes parameters as each handler does. Filter the QuerySet with them and return it:

```python
import msgspec

from django_bolt import BoltAPI, PageNumberPagination, paginate

from blog.models import Article

api = BoltAPI()


class ArticleItem(msgspec.Struct):
    id: int
    title: str


@api.get("/articles")
@paginate(PageNumberPagination)
async def list_articles(published: bool = True, search: str = "") -> list[ArticleItem]:
    queryset = Article.objects.filter(published=published).order_by("id")
    if search:
        queryset = queryset.filter(title__icontains=search)
    return queryset
```

```bash
curl "localhost:8000/articles?published=false"
```

```json
{"items": [{"id": 3, "title": "Draft"}], "total": 1, "has_next": false, "has_previous": false, "page": 1, "page_size": 100, "total_pages": 1}
```

The handler can be `async def` or `def`. Bolt calls a sync paginated handler on the event loop. Thus a sync handler must return a lazy QuerySet or a list, and must not run a query itself. Bolt runs the count and the page query on the ORM thread pool.

## Change the envelope

Set `response_class` to a generic `msgspec.Struct`, and override `build_response()`. Bolt uses `response_class[Item]` for the response type and the OpenAPI schema:

```python
from typing import Generic, TypeVar

import msgspec

from django_bolt import BoltAPI, PageNumberPagination, paginate

from blog.models import Article

api = BoltAPI()

T = TypeVar("T")


class Page(msgspec.Struct, Generic[T]):
    results: list[T]
    count: int
    next: int | None


class CountNextPagination(PageNumberPagination):
    page_size = 2
    response_class = Page

    def build_response(self, items, *, total, request, has_next, has_previous, **info):
        return Page(results=items, count=total, next=info["next_page"])


class ArticleItem(msgspec.Struct):
    id: int
    title: str


@api.get("/articles")
@paginate(CountNextPagination)
async def list_articles() -> list[ArticleItem]:
    return Article.objects.filter(published=True).order_by("id")
```

```json
{"results": [{"id": 1, "title": "Hello Bolt"}, {"id": 2, "title": "Async ORM"}], "count": 2, "next": null}
```

`build_response()` gets the items of the page and these keyword arguments: `total`, `request`, `has_next` and `has_previous`. `info` holds the fields of the class: `page`, `page_size`, `total_pages`, `next_page` and `previous_page` for `PageNumberPagination`; `limit` and `offset` for `LimitOffsetPagination`; `page_size` and `next_cursor` for `CursorPagination`.

For a new scheme, subclass `PaginationBase` and implement `get_page_params()` and `paginate_queryset()`. Both are `async`.

## Paginate a ViewSet

Set `pagination_class` on a `ViewSet`. Bolt then paginates its `list` action:

```python
from django_bolt import BoltAPI, PageNumberPagination, ReadOnlyModelViewSet
from django_bolt.serializers import Serializer

from blog.models import Article

api = BoltAPI()


class ArticleOut(Serializer):
    id: int
    title: str


class ArticlePages(PageNumberPagination):
    page_size = 2


@api.viewset("/articles")
class ArticleViewSet(ReadOnlyModelViewSet):
    queryset = Article.objects.order_by("id")
    serializer_class = ArticleOut
    pagination_class = ArticlePages
```

```bash
curl localhost:8000/articles
```

```json
{"items": [{"id": 1, "title": "Hello Bolt"}, {"id": 2, "title": "Async ORM"}], "total": 3, "has_next": true, "has_previous": false, "page": 1, "page_size": 2, "total_pages": 2, "next_page": 2}
```

A `list` method with its own `@paginate` keeps that decorator. A custom `@action` gets pagination only from its own `@paginate`. See [Class-based views](class-based-views.md).

## See also

- [Serializers](serializers.md)
- [Async ORM](async-orm.md)
- [Class-based views](class-based-views.md)
- [BoltAPI reference](../ref/api.md)
