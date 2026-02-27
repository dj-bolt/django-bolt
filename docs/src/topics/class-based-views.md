---
icon: lucide/boxes
---

# Class-Based Views

Django-Bolt supports class-based views for organizing related endpoints. This guide covers `APIView`, `ViewSet`, and `ModelViewSet`.

## APIView

Use `APIView` to group HTTP methods for a single resource:

```python
from django_bolt import BoltAPI
from django_bolt.views import APIView

api = BoltAPI()

@api.view("/hello")
class HelloView(APIView):
    async def get(self, request):
        return {"message": "Hello"}

    async def post(self, request, name: str):
        return {"message": f"Hello, {name}"}
```

This creates:

- `GET /hello` - Calls the `get` method
- `POST /hello` - Calls the `post` method

### Available methods

Implement any of these methods:

```python
@api.view("/resource")
class ResourceView(APIView):
    async def get(self, request):
        """Handle GET requests"""
        return {"method": "GET"}

    async def post(self, request):
        """Handle POST requests"""
        return {"method": "POST"}

    async def put(self, request):
        """Handle PUT requests"""
        return {"method": "PUT"}

    async def patch(self, request):
        """Handle PATCH requests"""
        return {"method": "PATCH"}

    async def delete(self, request):
        """Handle DELETE requests"""
        return {"method": "DELETE"}
```

### Class-level configuration

Set authentication and permissions at the class level:

```python
from django_bolt.auth import JWTAuthentication, IsAuthenticated

@api.view("/protected")
class ProtectedView(APIView):
    auth = [JWTAuthentication()]
    guards = [IsAuthenticated()]

    async def get(self, request):
        return {"user_id": request.user.id}
```

### Path parameters

Handle path parameters in your methods:

```python
@api.view("/users/{user_id}")
class UserView(APIView):
    async def get(self, request, user_id: int):
        return {"user_id": user_id}

    async def put(self, request, user_id: int, data: UserUpdate):
        return {"user_id": user_id, "updated": True}
```

## ViewSet

`ViewSet` provides a higher-level abstraction for CRUD operations:

```python
from django_bolt.views import ViewSet

@api.viewset("/items")
class ItemViewSet(ViewSet):
    async def list(self, request) -> list[dict]:
        """GET /items"""
        return [{"id": 1}, {"id": 2}]

    async def retrieve(self, request, pk: int) -> dict:
        """GET /items/{pk}"""
        return {"id": pk}

    async def create(self, request, item: ItemCreate) -> dict:
        """POST /items"""
        return {"id": 1, "created": True}

    async def update(self, request, pk: int, item: ItemUpdate) -> dict:
        """PUT /items/{pk}"""
        return {"id": pk, "updated": True}

    async def partial_update(self, request, pk: int, item: ItemPatch) -> dict:
        """PATCH /items/{pk}"""
        return {"id": pk, "patched": True}

    async def destroy(self, request, pk: int):
        """DELETE /items/{pk}"""
        return None
```

This creates:

| Method | URL | Action |
|--------|-----|--------|
| GET | `/items` | `list` |
| POST | `/items` | `create` |
| GET | `/items/{pk}` | `retrieve` |
| PUT | `/items/{pk}` | `update` |
| PATCH | `/items/{pk}` | `partial_update` |
| DELETE | `/items/{pk}` | `destroy` |

## ModelViewSet

`ModelViewSet` provides built-in Django ORM integration:

```python
from django_bolt import ModelViewSet, PageNumberPagination
from django_bolt.serializers import Serializer
from myapp.models import Article

class ArticleSchema(Serializer):
    id: int
    title: str
    content: str

class ArticleCreateSchema(Serializer):
    title: str
    content: str

@api.viewset("/articles")
class ArticleViewSet(ModelViewSet):
    queryset = Article.objects.all()
    serializer_class = ArticleSchema
    create_serializer_class = ArticleCreateSchema
    pagination_class = PageNumberPagination
```

### get_queryset

Override to customize the queryset:

```python
@api.viewset("/my-articles")
class MyArticleViewSet(ModelViewSet):
    queryset = Article.objects.all()

    async def get_queryset(self):
        qs = await super().get_queryset()
        return qs.filter(author_id=self.request.user.id)
```

### get_object

Get a single object by primary key:

```python
async def retrieve(self, request):
    article = await self.get_object()  # Raises 404 if not found
    return ArticleSchema.from_model(article)
```

### Custom lookup field

Use a different field for lookups:

```python
@api.viewset("/articles")
class ArticleViewSet(ModelViewSet):
    queryset = Article.objects.all()
    lookup_field = "slug"  # Use slug instead of pk

    async def retrieve(self, request):
        article = await self.get_object()
        return {"slug": article.slug}
```

### Serializer Priority

| Action                      | Validation                                                                                | Response                                            |
|-----------------------------|-------------------------------------------------------------------------------------------|-----------------------------------------------------|
| `list`                      |                                                                                           | `list_serializer_class` </br> or `serializer_class` |
| `retrieve`                  |                                                                                           | `serializer_class`                                  |
| `create`                    | `create_serializer_class` </br> or `serializer_class`                                     | `serializer_class`                                  |
| `update` / `partial_update` | `update_serializer_class` </br> or `create_serializer_class`  </br> or `serializer_class` | `serializer_class`                                  |

!!! warning

    If there is no available `serializer_class` or return type annotation for `list` / `retrieve` / `create` / `update` / `partial_update`, a `ValueError` will be raised.

## Custom actions

Add custom actions using the `@action` decorator to create endpoints beyond standard CRUD operations.

### Basic actions

```python
from django_bolt import action

@api.viewset("/articles")
class ArticleViewSet(ViewSet):
    queryset = Article.objects.all()

    @action(methods=["GET"], detail=False)
    async def published(self, request):
        """Collection action: GET /articles/published"""
        articles = []
        async for article in Article.objects.filter(is_published=True):
            articles.append({"id": article.id, "title": article.title})
        return articles

    @action(methods=["POST"], detail=True)
    async def publish(self, request, pk: int):
        """Instance action: POST /articles/{pk}/publish"""
        article = await self.get_object(pk)
        article.is_published = True
        await article.asave()
        return {"published": True, "article_id": pk}
```

### Action parameters

| Parameter        | Required | Description                                                                              |
|------------------|----------|------------------------------------------------------------------------------------------|
| `methods`        | yes      | List of HTTP methods: `["GET"]`, `["POST"]`, etc.                                        |
| `detail`         | yes      | `True` for instance actions (`/{pk}/action`), `False` for collection actions (`/action`) |
| `path`           | no       | Custom URL path (defaults to function name)                                              |
| `auth`           | no       | List of authentication backends (overrides class-level `auth`)                           |
| `guards`         | no       | List of permission guards (overrides class-level `guards`)                               |
| `response_model` | no       | Response model for serialization                                                         |
| `status_code`    | no       | HTTP status code                                                                         |
| `tags`           | no       | List of tags for OpenAPI documentation                                                   |
| `summary`        | no       | Summary for OpenAPI documentation                                                        |
| `description`    | no       | Detailed description for OpenAPI documentation                                           |

### Custom path

Override the URL path:

```python
@action(methods=["POST"], detail=True, path="custom-action-name")
async def some_method_name(self, request, pk: int):
    """POST /articles/{pk}/custom-action-name"""
    return {"action": "custom-action-name", "article_id": pk}
```

### Actions with query parameters

```python
@action(methods=["GET"], detail=False)
async def search(self, request, query: str, limit: int = 10):
    """GET /articles/search?query=xxx&limit=5"""
    articles = []
    async for article in Article.objects.filter(title__icontains=query)[:limit]:
        articles.append({"id": article.id, "title": article.title})
    return {"query": query, "limit": limit, "results": articles}
```

### Actions with request body

```python
import msgspec

class StatusUpdate(msgspec.Struct):
    is_published: bool

@action(methods=["POST"], detail=True, path="status")
async def update_status(self, request, pk: int, data: StatusUpdate):
    """POST /articles/{pk}/status with JSON body"""
    article = await self.get_object(pk)
    article.is_published = data.is_published
    await article.asave()
    return {"updated": True, "is_published": article.is_published}
```

### Multiple methods on same path

Create separate actions for different HTTP methods on the same path:

```python
@action(methods=["GET"], detail=True, path="status")
async def get_status(self, request, pk: int):
    """GET /articles/{pk}/status"""
    article = await self.get_object(pk)
    return {"is_published": article.is_published}

@action(methods=["POST"], detail=True, path="status")
async def update_status(self, request, pk: int, data: StatusUpdate):
    """POST /articles/{pk}/status"""
    article = await self.get_object(pk)
    article.is_published = data.is_published
    await article.asave()
    return {"updated": True}
```

### Custom lookup field with actions

Actions respect the ViewSet's `lookup_field`:

```python
@api.viewset("/articles")
class ArticleViewSet(ViewSet):
    queryset = Article.objects.all()
    lookup_field = 'id'  # Use 'id' instead of 'pk'

    async def retrieve(self, request):
        """GET /articles/{id}"""
        article = await self.get_object()
        return {"id": article.id, "title": article.title}

    @action(methods=["POST"], detail=True)
    async def feature(self, request, id: int):
        """POST /articles/{id}/feature"""
        return {"featured": True, "article_id": id}
```

### Important: Actions require api.viewset()

The `@action` decorator only works with `api.viewset()`, not `api.view()`:

```python
# CORRECT: Use api.viewset()
@api.viewset("/articles")
class ArticleViewSet(ViewSet):
    @action(methods=["POST"], detail=False)
    async def custom_action(self, request):
        return {"ok": True}

# WRONG: Will raise ValueError
@api.view("/articles", methods=["GET"])
class ArticleView(ViewSet):
    @action(methods=["POST"], detail=False)  # Error!
    async def custom_action(self, request):
        return {"ok": True}
```

## Mixins

Use mixins to compose functionality:

```python
from django_bolt.views import (
    ListMixin,
    RetrieveMixin,
    CreateMixin,
    UpdateMixin,
    PartialUpdateMixin,
    DestroyMixin,
    ViewSet,
)

# Read-only viewset
@api.viewset("/readonly-items")
class ReadOnlyItemViewSet(ListMixin, RetrieveMixin, ViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer

# Full CRUD viewset
@api.viewset("/items")
class ItemViewSet(
    ListMixin,
    RetrieveMixin,
    CreateMixin,
    UpdateMixin,
    PartialUpdateMixin,
    DestroyMixin,
    ViewSet,
):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
```

Available mixins:

| Mixin | Action | URL | Method |
|-------|--------|-----|--------|
| `ListMixin` | `list` | `/items` | GET |
| `RetrieveMixin` | `retrieve` | `/items/{pk}` | GET |
| `CreateMixin` | `create` | `/items` | POST |
| `UpdateMixin` | `update` | `/items/{pk}` | PUT |
| `PartialUpdateMixin` | `partial_update` | `/items/{pk}` | PATCH |
| `DestroyMixin` | `destroy` | `/items/{pk}` | DELETE |

## ReadOnlyModelViewSet

A convenient shortcut for read-only access:

```python
from django_bolt.views import ReadOnlyModelViewSet

@api.viewset("/public-articles")
class PublicArticleViewSet(ReadOnlyModelViewSet):
    queryset = Article.objects.filter(published=True)
    serializer_class = ItemSerializer
```

This provides only `list` and `retrieve` actions.

## Sync handlers

APIView support synchronous handlers:

```python
@api.view("/sync-resource")
class SyncResourceView(APIView):
    def get(self, request):  # Note: not async
        return {"sync": True}
```

`@action` decorators on `ViewSet` and `ModelViewSet` also support synchronous handlers:

```python
@api.viewset("/sync-items")
class SyncItemViewSet(ViewSet):
    @action(methods=["GET"], detail=False)
    def sync(self, request):
        return {"sync": True}
```

Sync handlers are automatically wrapped to run in a thread pool.

!!! warning

    CRUD methods on ViewSets (`list`, `retrieve`, `create`, `update`, `partial_update`, `destroy`) currently do not support synchronous handlers.
