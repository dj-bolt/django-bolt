---
description: Group handlers in classes with APIView, ViewSet and ModelViewSet, add custom actions with @action, and register them with api.view() and api.viewset().
---

# Class-based views

A class-based view groups the handlers of one resource in a class. Bolt has three base classes:

- `APIView`: one path, one method for each HTTP method.
- `ViewSet`: a collection path and a detail path, with methods named after actions, such as `list` and `retrieve`.
- `ModelViewSet`: a `ViewSet` with ready create, read, update and delete actions for a Django model.

Each method is a handler. It takes typed parameters, dependencies and a return type, as a function handler does.

## Write an APIView

Register the class with `@api.view(path)`. Bolt makes one route for each method of the class that has the name of an HTTP method:

```python
from django_bolt import APIView, BoltAPI

from blog.models import Article

api = BoltAPI()


@api.view("/articles/{article_id}/title")
class ArticleTitleView(APIView):
    async def get(self, article_id: int) -> dict:
        article = await Article.objects.aget(id=article_id)
        return {"title": article.title}

    async def put(self, article_id: int, title: str) -> dict:
        updated = await Article.objects.filter(id=article_id).aupdate(title=title)
        return {"updated": updated}
```

```bash
curl localhost:8000/articles/1/title
```

```json
{"title": "Hello Bolt"}
```

```bash
curl -X PUT "localhost:8000/articles/1/title?title=Renamed"
```

```json
{"updated": 1}
```

The method names are `get`, `post`, `put`, `patch`, `delete`, `head`, `options` and `query`. A request with another method gets `405`.

A method can take the request as a parameter named `request` or `req`. It can also read `self.request`.

!!! warning
    Bolt makes one instance of the view class for each route, at startup. All requests share it. Do not keep request data on `self`. Use local variables, `self.request` and dependencies.

### Set options for all methods

Class attributes apply to each method of the class:

```python
from django_bolt import APIView, BoltAPI, Request
from django_bolt.auth import IsAuthenticated, JWTAuthentication

api = BoltAPI()


@api.view("/me")
class ProfileView(APIView):
    auth = [JWTAuthentication()]
    guards = [IsAuthenticated()]

    async def get(self, request: Request) -> dict:
        user = await request.auser()
        return {"username": user.username}
```

| Attribute | Default | Effect |
| --- | --- | --- |
| `auth` | `None` | The authentication backends of each route. |
| `guards` | `None` | The guards of each route. |
| `status_code` | `None` | The status code of a successful response. |
| `validate_response` | `None` | Turn response validation on or off. |
| `include_in_schema` | `None` | `False` leaves the routes out of the OpenAPI schema. |
| `http_method_names` | all methods above | The method names that Bolt looks for. |

`None` means "use the value of the API". An argument of `api.view()` wins over the class attribute.

`api.view()` takes `path`, `name`, `methods`, `guards`, `auth`, `status_code`, `validate_response`, `tags`, `include_in_schema` and `deprecated`. Use `methods=["GET"]` to register some methods only.

## Write a ViewSet

A `ViewSet` serves a collection path and a detail path. Register it with `@api.viewset(path)`. Bolt makes a route for each action method that the class has:

| Method | Route | Status |
| --- | --- | --- |
| `list` | `GET /path` | `200` |
| `create` | `POST /path` | `201` |
| `retrieve` | `GET /path/{pk}` | `200` |
| `update` | `PUT /path/{pk}` | `200` |
| `partial_update` | `PATCH /path/{pk}` | `200` |
| `destroy` | `DELETE /path/{pk}` | `204` |

```python
from django_bolt import BoltAPI, ViewSet
from django_bolt.serializers import Serializer

from blog.models import Article

api = BoltAPI()


class ArticleOut(Serializer):
    id: int
    title: str


@api.viewset("/articles")
class ArticleViewSet(ViewSet):
    queryset = Article.objects.order_by("id")
    serializer_class = ArticleOut

    async def list(self) -> list[ArticleOut]:
        return await self.get_queryset()

    async def retrieve(self, pk: int) -> ArticleOut:
        return await self.get_object(pk)
```

```bash
curl localhost:8000/articles/2
```

```json
{"id": 2, "title": "Async ORM"}
```

```bash
curl localhost:8000/articles/99
```

```json
{"detail": "Not found"}
```

`get_queryset()` returns a new copy of `queryset` for each call. `get_object()` returns the row with the lookup value, or raises `HTTPException` with `404`. A value that the field cannot hold, such as `abc` for an integer key, is also not found.
Here `retrieve` declares `pk: int`, so Bolt checks the type before the handler runs. `/articles/abc` then gets `422`.

`get_object()` takes one of these forms:

- `get_object()` reads the lookup value from the path of the request.
- `get_object(value)` uses `value` for the lookup field.
- `get_object(field=value)` filters by the keyword arguments.

### Set the lookup field

The lookup field names the path parameter and the model field that `get_object()` filters on. The default is `pk`. Set `lookup_field` on the class, or pass it to `api.viewset()`. The registration argument wins:

<!-- fragment -->
```python
@api.viewset("/articles", lookup_field="id")
class ArticleViewSet(ViewSet):
    ...
```

The detail routes are then `/articles/{id}`. The value must be a model field, because `get_object()` filters on it.

### Filter the QuerySet for each request

Override `get_queryset()` or `filter_queryset()`. Both are `async`. Read the request from `self.request`, and the action name from `self.action`:

<!-- fragment -->
```python
    async def get_queryset(self):
        queryset = await super().get_queryset()
        if self.action == "list" and "published" in self.request.query:
            queryset = queryset.filter(published=self.request.query["published"] == "true")
        return queryset
```

`get_object()` uses `get_queryset()` and then `filter_queryset()`. Thus a filter there also limits the detail routes.

## Add a custom action

Decorate a method with `@action`. `detail=True` makes a route under the detail path. `detail=False` makes a route under the collection path:

```python
from django_bolt import BoltAPI, ViewSet, action
from django_bolt.serializers import Serializer

from blog.models import Article

api = BoltAPI()


class ArticleOut(Serializer):
    id: int
    title: str


@api.viewset("/articles")
class ArticleViewSet(ViewSet):
    queryset = Article.objects.order_by("id")
    serializer_class = ArticleOut

    @action(methods=["POST"], detail=True)
    async def publish(self, pk: int) -> ArticleOut:
        article = await self.get_object(pk)
        article.published = True
        await article.asave(update_fields=["published"])
        return article

    @action(methods=["GET"], detail=False)
    async def drafts(self) -> list[ArticleOut]:
        return (await self.get_queryset()).filter(published=False)
```

```bash
curl -X POST localhost:8000/articles/3/publish
```

```json
{"id": 3, "title": "Draft"}
```

The routes are `POST /articles/{pk}/publish` and `GET /articles/drafts`.

`@action` takes these arguments:

| Argument | Default | Effect |
| --- | --- | --- |
| `methods` | required | The HTTP methods, for example `["GET", "POST"]`. |
| `detail` | required | `True` for `/path/{pk}/<name>`, `False` for `/path/<name>`. |
| `path` | the method name | The last path segment. |
| `name` | the method name | The suffix of the route name. |
| `auth`, `guards`, `status_code`, `validate_response`, `tags` | `None` | Options of the route. Each wins over the class and the registration. |
| `response_model` | none | The response type, in place of the return type. |
| `summary`, `description`, `deprecated` | `None`, `None`, `False` | OpenAPI fields of the operation. |

`@action` works with `api.viewset()` only. `api.view()` raises `ValueError` for a class with an action.

Bolt makes a new instance of the class for each request to a custom action.

## Use a ModelViewSet

`ModelViewSet` implements the six actions for the model of `queryset`. Give it the model and a serializer:

```python
from django_bolt import BoltAPI, ModelViewSet, PageNumberPagination
from django_bolt.serializers import Serializer, field

from blog.models import Article

api = BoltAPI()


class ArticleSerializer(Serializer):
    id: int = field(read_only=True, default=0)
    title: str
    body: str
    author_id: int
    published: bool = False


class ArticleListItem(Serializer):
    id: int
    title: str


class ArticlePages(PageNumberPagination):
    page_size = 2


@api.viewset("/articles")
class ArticleViewSet(ModelViewSet):
    queryset = Article.objects.order_by("id")
    serializer_class = ArticleSerializer
    list_serializer_class = ArticleListItem
    pagination_class = ArticlePages
```

```bash
curl -X POST localhost:8000/articles -H "Content-Type: application/json" \
  -d '{"id": 99, "title": "New", "body": "Text", "author_id": 2}'
```

```json
{"id": 4, "title": "New", "body": "Text", "author_id": 2, "published": false}
```

```bash
curl -X PATCH localhost:8000/articles/4 -H "Content-Type: application/json" -d '{"published": true}'
```

```json
{"id": 4, "title": "New", "body": "Text", "author_id": 2, "published": true}
```

```bash
curl localhost:8000/articles
```

```json
{"items": [{"id": 1, "title": "Hello Bolt"}, {"id": 2, "title": "Async ORM"}], "total": 4, "has_next": true, "has_previous": false, "page": 1, "page_size": 2, "total_pages": 2, "next_page": 2}
```

`DELETE /articles/4` answers `204` with no body.

The actions work like this:

- `create` validates the body with `create_serializer_class`, or `serializer_class`. It does not set `read_only` fields. It does not set the primary key when the database makes it. A unique value that a row already has gives `409`.
- `update` (PUT) validates the full body with `update_serializer_class`, then `create_serializer_class`, then `serializer_class`. It writes each field except `read_only` fields, the primary key and the lookup field.
- `partial_update` (PATCH) writes only the fields of the body. It validates the merged object with the update serializer.
- `list` uses `list_serializer_class`, or `serializer_class`, for each item. With `pagination_class`, Bolt paginates it.
- `retrieve`, `create`, `update` and `partial_update` return the object with `serializer_class`.
- `destroy` deletes the row and answers `204`.

`create`, `update` and `partial_update` pass the serializer fields to the model with their names. Name a foreign key field `author_id`, not `author`, so that the model gets the key.

`ReadOnlyModelViewSet` has only `list` and `retrieve`. The mixins `ListMixin`, `RetrieveMixin`, `CreateMixin`, `UpdateMixin`, `PartialUpdateMixin` and `DestroyMixin` give one action each. Combine them with `ViewSet`:

<!-- fragment -->
```python
from django_bolt import CreateMixin, ListMixin, ViewSet


@api.viewset("/comments")
class CommentViewSet(ListMixin, CreateMixin, ViewSet):
    ...
```

### Customize the write steps

Override these `async` methods to change how the actions save:

- `perform_create(obj)` saves a new object. The default is `await obj.asave(force_insert=True)`.
- `perform_update(obj)` saves a changed object. The default is `await obj.asave()`.
- `perform_destroy(obj)` deletes an object. The default is `await obj.adelete()`.

<!-- fragment -->
```python
    async def perform_create(self, obj):
        obj.author_id = (await self.request.auser()).id
        await obj.asave(force_insert=True)
```

Override `get_serializer_class(action=None)` to choose a serializer at run time. Bolt also calls it at startup, with `self.action` set to `"list"` or `"retrieve"`, to find the response types. `self.request` is not available at startup.

## Name the routes

Bolt names each route of a ViewSet `<base>-<action>`. The base is the `name` argument of `api.viewset()`, or the class name. For example, `ArticleViewSet-list` and `ArticleViewSet-publish`. The routes of an `APIView` get the class name, or the `name` argument. See [Routing](routing.md) for reverse lookups.

Bolt has no router objects for ViewSets, as Django REST Framework has. `api.viewset()` makes the routes. A `Router` holds function routes only.

## See also

- [Views reference](../ref/views.md)
- [Serializers](serializers.md)
- [Pagination](pagination.md)
- [Dependencies](dependencies.md)
- [Permissions](permissions.md)
- [Routing](routing.md)
