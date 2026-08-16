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
