---
icon: lucide/list
---

# Pagination

Django-Bolt provides three pagination styles for handling large datasets efficiently.

## PageNumber Pagination

Classic page-based pagination:

```python
from django_bolt import BoltAPI, PageNumberPagination, paginate

api = BoltAPI()

class ArticlePagination(PageNumberPagination):
    page_size = 20
    max_page_size = 100
    page_size_query_param = "page_size"  # Allow client to customize

@api.get("/articles")
@paginate(ArticlePagination)
async def list_articles(request) -> list[ArticleSerializer]:
    return Article.objects.all()
```

Response:

```json
{
  "count": 150,
  "page": 1,
  "page_size": 20,
  "total_pages": 8,
  "has_next": true,
  "has_previous": false,
  "next_page": 2,
  "previous_page": null,
  "items": [...]
}
```

Query: `/articles?page=2&page_size=10`

## LimitOffset Pagination

Flexible offset-based pagination:

```python
from django_bolt import LimitOffsetPagination, paginate

@api.get("/articles", response_model=list[ArticleSerializer])
@paginate(LimitOffsetPagination)
async def list_articles(request):
    return Article.objects.all()
```

Query: `/articles?limit=10&offset=20`

Response includes `limit`, `offset`, `total`, `has_next`, `has_previous`.

## Cursor Pagination

Efficient pagination for large datasets and real-time feeds:

```python
from django_bolt import CursorPagination, paginate

class ArticlePagination(CursorPagination):
    page_size = 20
    ordering = "-created_at"  # Required: field to paginate by

@api.get("/articles")
@paginate(ArticlePagination)
async def list_articles(request) -> list[ArticleSerializer]:
    return Article.objects.all()
```

Query: `/articles?cursor=eyJ2IjoxMDB9`

### Ordering and cursor stability

Cursor pagination requires a **stable, unique ordering of the result rows**.
For ordinary model QuerySets, a single ordering string automatically adds the
model primary key as a tie-breaker unless a safe unique field is known:

```python
class ArticlePagination(CursorPagination):
    ordering = "-created_at"  # SQL: ORDER BY created_at DESC, id DESC
```

The default `ordering = "-id"` remains a fast single-key path. A concrete,
non-null, non-JSON model field declared `unique=True` also uses one ordering
key and a scalar cursor, provided the query has no joins that can multiply
source rows. A UNIQUE index that exists only in the database is not inferred
from model metadata. Earlier two-key cursors for such a field remain accepted.

For advanced queries, a **tuple** makes the ordering fully explicit. Bolt does
not append the primary key; provide enough keys to uniquely identify each
*result row* (not necessarily a source model):

```python
class ReportPagination(CursorPagination):
    page_size = 20
    ordering = ("category", "brand")  # No implicit PK

# The combination (category, brand) identifies each result:
report = Product.objects.values("category", "brand").annotate(total=Count("id"))
```

A one-element tuple such as `("price",)` also opts out of automatic PK
tie-breaking. It is **only safe if the selected key is unique**. Mixing
directions, for example `("category", "-created_at", "id")`, is supported.
Changing the ordering between requests invalidates existing cursor positions.
Cursor pagination does not provide a snapshot across concurrent updates to
ordering fields.

For `values()` and `values_list()`, include the explicitly selected ordering
fields in the projection. In the automatic mode, Bolt can select a hidden PK
without exposing it in the response. Foreign keys use the stored `author_id`
value rather than fetching the related object; when related-model ordering
would change the meaning, use `author_id` explicitly.

For `DISTINCT` and `GROUP BY` projections, automatic PK tie-breaking
could change the meaning of the SQL result. Bolt preserves the original
single-key grouped/distinct behavior when the result key is unique, and
accepts explicit tuples for multi-key projections. **The developer must ensure
the whole tuple uniquely identifies each result row.** An aggregate may be
used as an ordering key, but additional grouped keys may be needed to avoid
ties. Unsupported projection shapes report a clear error rather than
silently losing rows.

SQL `NULL` is a valid cursor position. A JSONField may contain SQL `NULL`
or JSON `null`; Django reads both as Python `None`. Bolt checks a nullable
JSONField boundary only when it needs to distinguish them. This can add
one query for each ambiguous JSON key at a page boundary. Ordinary pages
need no extra query.

For an explicit tuple over a nullable JSONField, projected results must
include the primary key. Otherwise Bolt cannot identify the boundary row
to distinguish SQL `NULL` from JSON `null`, so it raises `ValueError` before
fetching data. A direct non-nullable JSONField needs no extra lookup.
You can also order by a typed, non-JSON key. Bolt does not support JSONField
ordering in grouped or distinct projections without a reliable row key.

The API still has **one opaque `cursor`/ `next_cursor` mechanism**, not
separate pagination classes or query parameters. New non-PK positions use
one internal keyset format; legacy scalar and previously emitted composite
cursors remain accepted. Legacy scalar cursors cannot resume *inside* a
group of tied ordering values.

Django does not permit applying `filter()` after
`union()`, `intersection()`, or `difference()`. Such combined QuerySets
are rejected up front instead of emitting a `next_cursor` that cannot
fetch the next page. Use page-number/limit-offset pagination or refactor
the combined query if it must be paginated.

For deep PostgreSQL pages, a B-tree index aligned with the selected ordering
columns (including the PK in automatic mode) is recommended. Bolt uses
row-value comparisons for compatible equal-direction, non-null boundaries
and a NULL-aware lexicographic predicate otherwise. This does not require
a separate count query.

## Specifying the Serializer

Two equivalent ways to specify which serializer to use:

```python
# Option 1: Return type annotation (recommended)
@api.get("/articles")
@paginate(ArticlePagination)
async def list_articles(request) -> list[ArticleSerializer]:
    return Article.objects.all()

# Option 2: response_model parameter
@api.get("/articles", response_model=list[ArticleSerializer])
@paginate(ArticlePagination)
async def list_articles(request):
    return Article.objects.all()
```

Both approaches work identically. The serializer automatically filters fields - only declared fields are included in the response.

## ViewSet with Pagination

```python
from django_bolt.views import ViewSet

@api.viewset("/articles")
class ArticleViewSet(ViewSet):
    queryset = Article.objects.all()
    pagination_class = ArticlePagination

    async def list(self, request) -> list[ArticleSerializer]:
        return await self.get_queryset()
```

`pagination_class` is applied automatically to `list()` on `ViewSet`,
`ReadOnlyModelViewSet`, and `ModelViewSet`. Keep using `@paginate(...)` for
function-based views or when you want a method-specific override.

## ModelViewSet with Pagination

```python
from django_bolt.views import ModelViewSet

@api.viewset("/articles")
class ArticleViewSet(ModelViewSet):
    queryset = Article.objects.all()
    serializer_class = ArticleDetailSerializer  # For detail views
    list_serializer_class = ArticleListSerializer  # For list view
    pagination_class = ArticlePagination  # Automatically applied to list()
```

## Custom response envelope

The default envelope is `PaginatedResponse[T]`. It has `items`, `total`,
`has_next`, `has_previous` and the fields of the pagination scheme. You can use
a different shape, for example the DRF shape `count/next/previous/results`. Set
`response_class` to a generic `Serializer` or `msgspec.Struct`. Then override
`build_response()`. The response body and the OpenAPI schema
(`DRFPage_ArticleSerializer_`) use the new shape:

```python
from typing import Generic, TypeVar

T = TypeVar("T")

class DRFPage(Serializer, Generic[T]):
    count: int
    next: str | None
    previous: str | None
    results: list[T]

class DRFPagination(PageNumberPagination):
    page_size = 20
    response_class = DRFPage

    def build_response(self, items, *, total, request, page, page_size, has_next, has_previous, **info):
        base = f"{request.path}?page_size={page_size}&page="
        return DRFPage(
            count=total,
            next=f"{base}{page + 1}" if has_next else None,
            previous=f"{base}{page - 1}" if has_previous else None,
            results=items,
        )
```

`build_response()` gets the serialized `items`, the `total`, the `request` and
the `has_next` and `has_previous` flags. It also gets the fields of the scheme
as keyword arguments:

- Page number: `page`, `page_size`, `total_pages`, `next_page`, `previous_page`
- Limit/offset: `limit`, `offset`
- Cursor: `page_size`, `next_cursor`

## Performance Considerations

### Avoiding N+1 Queries

If the item type is a Bolt `Serializer`, Bolt applies the loading plan of the
serializer to the page QuerySet. The plan uses `select_related` for ForeignKey
and OneToOne fields, `prefetch_related` for many-valued relations, and
`Config.annotations`. Nested relations cost a small, fixed number of queries
for each page. See
[Loading a QuerySet with from_models()](serializers.md#loading-a-queryset-with-from_models).

Bolt keeps the loads that you apply. Bolt does not apply them a second time.
This code is correct:

```python
@api.get("/articles")
@paginate(ArticlePagination)
async def list_articles(request) -> list[ArticleSerializer]:
    return Article.objects.select_related("author").prefetch_related("tags")
```

### Cursor vs PageNumber for Large Datasets

For tables with millions of rows, prefer `CursorPagination` over `PageNumberPagination`. Cursor pagination uses indexed columns for efficient seeking, while page number pagination requires counting total rows.
