---
description: Reference for APIView, ViewSet, ModelViewSet, the mixins, the action decorator, api.view() and api.viewset().
---

# Views reference

Import each class and `action` from `django_bolt`. For a task-based introduction, see [Class-based views](../topics/class-based-views.md).

## APIView

<!-- fragment -->
```python
class APIView
```

A class whose methods are handlers for one path. Each method with the name of an HTTP method becomes a route when you register the class with `api.view()`.

Bolt makes one instance of the class for each route, at registration. All requests to that route share the instance. A keyword argument of `as_view()` becomes an attribute of that instance.

A method takes handler parameters, dependencies and a return type, as a function handler does. It can be `async def` or `def`. A parameter named `request` or `req` gets the request.

### Class attributes

| Attribute | Type | Default | Description |
| --- | --- | --- | --- |
| `http_method_names` | `tuple[str, ...]` | `("get", "post", "put", "patch", "delete", "head", "options", "query")` | The method names that Bolt registers. |
| `auth` | `list \| None` | `None` | Authentication backends for each route of the class. |
| `guards` | `list \| None` | `None` | Guards for each route of the class. |
| `status_code` | `int \| None` | `None` | The success status code of each route. |
| `validate_response` | `bool \| None` | `None` | Turn response validation on or off for each route. |
| `include_in_schema` | `bool \| None` | `None` | `False` leaves the routes out of the OpenAPI schema. |

`None` means: use the value of the next outer layer. The order is the argument of `api.view()` or `api.viewset()`, then the class attribute, then the `BoltAPI`.

### APIView.request

<!-- fragment -->
```python
@property
def request(self) -> Request
```

The request of the current call. Read it in a method or in a helper method. It raises `LookupError` outside a request.

### APIView.as_view()

<!-- fragment -->
```python
@classmethod
def as_view(cls, method: str, action: str | None = None, **initkwargs: Any) -> Callable
```

Return the handler function for one HTTP method. `api.view()` and `api.viewset()` call it. You call it only to register a method by hand.

**Parameters:**

- `method`: the HTTP method name, in any case.
- `action`: a ViewSet action name, such as `"list"`. Bolt looks for a method with that name first, then for the method name.
- `**initkwargs`: attributes of the view instance of this handler.

**Errors:** `ValueError` when `method` is not in `http_method_names`, or the class has no such method.

### APIView.get_allowed_methods()

<!-- fragment -->
```python
@classmethod
def get_allowed_methods(cls) -> set[str]
```

Return the upper-case HTTP methods that the class implements.

## ViewSet

<!-- fragment -->
```python
class ViewSet[T: Model](APIView)
```

A class whose action methods serve a collection path and a detail path. Register it with `api.viewset()`. A subclass implements the actions that it needs:

| Method | Route | Default status |
| --- | --- | --- |
| `list` | `GET {path}` | `200` |
| `create` | `POST {path}` | `201` |
| `retrieve` | `GET {path}/{lookup_field}` | `200` |
| `update` | `PUT {path}/{lookup_field}` | `200` |
| `partial_update` | `PATCH {path}/{lookup_field}` | `200` |
| `destroy` | `DELETE {path}/{lookup_field}` | `204` |

An attribute with an action name that is not a function, such as `destroy = None`, makes no route.

For a standard action with no return type and no `response_model`, Bolt takes the response type from `get_serializer_class()`: `list[...]` for `list`, the class for the others except `destroy`.

### Class attributes

ViewSet has the attributes of `APIView`, and these:

| Attribute | Type | Default | Description |
| --- | --- | --- | --- |
| `queryset` | `QuerySet \| None` | `None` | The base QuerySet. Each read of `self.queryset` returns a new copy. |
| `serializer_class` | `type[Serializer] \| None` | `None` | The default serializer of each action. |
| `list_serializer_class` | `type[Serializer] \| None` | `None` | The serializer of `list`. |
| `create_serializer_class` | `type[Serializer] \| None` | `None` | The input serializer of `create`, and of `update` and `partial_update` when `update_serializer_class` is `None`. |
| `update_serializer_class` | `type[Serializer] \| None` | `None` | The input serializer of `update` and `partial_update`. |
| `lookup_field` | `str` | `"pk"` | The path parameter of the detail routes, and the model field that `get_object()` filters on. |
| `pagination_class` | `type[PaginationBase] \| None` | `None` | Paginate the `list` action. A `list` method with its own `@paginate` keeps it. |

### ViewSet.action

<!-- fragment -->
```python
@property
def action(self) -> str
```

The action name of the current call, for example `"list"`, `"retrieve"` or the name of a custom action.

### ViewSet.get_queryset()

<!-- fragment -->
```python
async def get_queryset(self) -> QuerySet[T]
```

Return a new copy of `queryset`. Override it to filter by the request or the user.

**Errors:** `ValueError` when the class has no `queryset`.

### ViewSet.filter_queryset()

<!-- fragment -->
```python
async def filter_queryset(self, queryset: QuerySet[T]) -> QuerySet[T]
```

Return the QuerySet with filters. The default returns it unchanged. The `list` action of `ListMixin` and `get_object()` call it.

### ViewSet.get_object()

<!-- fragment -->
```python
async def get_object(self, *args: Any, **lookup_kwargs: Any) -> T
```

Return one row from `get_queryset()` and `filter_queryset()`.

- With no arguments, the lookup is `lookup_field` equal to the path parameter of the same name.
- With one positional argument, the lookup is `lookup_field` equal to that value.
- With keyword arguments, they are the lookup.

**Errors:**

- `HTTPException` with `404` and `"Not found"`: no row matches, or the field cannot hold the value.
- `HTTPException` with `400`: no arguments, and the path has no `lookup_field` parameter.
- `TypeError`: more than one positional argument, or positional and keyword arguments together.
- `MultipleObjectsReturned` from Django: more than one row matches.

### ViewSet.get_serializer_class()

<!-- fragment -->
```python
def get_serializer_class(self, action: str | None = None) -> type[Serializer]
```

Return the serializer of an action. `action` defaults to `self.action`. The default picks:

| Action | Serializer |
| --- | --- |
| `list` | `list_serializer_class`, else `serializer_class` |
| `create` | `create_serializer_class`, else `serializer_class` |
| `update`, `partial_update` | `update_serializer_class`, else `create_serializer_class`, else `serializer_class` |
| other | `serializer_class` |

Bolt also calls it at registration, with `self.action` set to `"list"` or `"retrieve"`. `self.request` is not available then.

**Errors:** `ValueError` when the picked attribute and `serializer_class` are `None`.

### ViewSet.perform_create(), perform_update() and perform_destroy()

<!-- fragment -->
```python
async def perform_create(self, obj: T) -> None
async def perform_update(self, obj: T) -> None
async def perform_destroy(self, obj: T) -> None
```

Save or delete an object for the mixin actions. The defaults are `await obj.asave(force_insert=True)`, `await obj.asave()` and `await obj.adelete()`.

## Mixins

Each mixin gives one action. Combine mixins with `ViewSet`, for example `class CommentViewSet(ListMixin, CreateMixin, ViewSet)`. Each action method takes the request only, and reads the body itself.

| Mixin | Method | Behavior |
| --- | --- | --- |
| `ListMixin` | `list(request)` | Returns `filter_queryset(get_queryset())`. |
| `RetrieveMixin` | `retrieve(request)` | Returns `get_object()`. |
| `CreateMixin` | `create(request)` | Validates the body with `get_serializer_class("create")`. Makes a model instance from the fields, with no `read_only` fields and no primary key that the database makes. Calls `perform_create()`. Returns the object. An `IntegrityError` gives `409`. |
| `UpdateMixin` | `update(request)` | Validates the full body with `get_serializer_class("update")`. Sets each field except `read_only` fields, the primary key and `lookup_field`. Calls `perform_update()`. Returns the object. |
| `PartialUpdateMixin` | `partial_update(request)` | Takes a JSON object. Sets the fields of the body, with the same exceptions as `update`. Validates the merged object with `get_serializer_class("update")`. Calls `perform_update()`. Returns the object. A body with no known field returns the object unchanged. |
| `DestroyMixin` | `destroy(request)` | Calls `get_object()` and `perform_destroy()`. The response is `204` with no body. |

A body that is not valid gives `422`. The mixins pass the fields to the model with their Python names. A field for a `ForeignKey` must thus have the name of the column attribute, for example `author_id`.

## ModelViewSet and ReadOnlyModelViewSet

<!-- fragment -->
```python
class ModelViewSet(ListMixin, RetrieveMixin, CreateMixin, UpdateMixin, PartialUpdateMixin, DestroyMixin, ViewSet)
class ReadOnlyModelViewSet(ListMixin, RetrieveMixin, ViewSet)
```

`ModelViewSet` has the six actions. `ReadOnlyModelViewSet` has `list` and `retrieve`. Set `queryset` and `serializer_class`.

**Example:**

```python
from django_bolt import BoltAPI, ModelViewSet
from django_bolt.serializers import Serializer, field

from blog.models import Article

api = BoltAPI()


class ArticleSerializer(Serializer):
    id: int = field(read_only=True, default=0)
    title: str
    body: str
    author_id: int


@api.viewset("/articles")
class ArticleViewSet(ModelViewSet):
    queryset = Article.objects.order_by("id")
    serializer_class = ArticleSerializer
```

## action()

<!-- fragment -->
```python
def action(
    methods: list[str],
    detail: bool,
    path: str | None = None,
    *,
    name: str | None = None,
    auth: list[Any] | None = None,
    guards: list[Any] | None = None,
    response_model: Any = <unset>,
    status_code: int | None = None,
    validate_response: bool | None = None,
    tags: list[str] | None = None,
    summary: str | None = None,
    description: str | None = None,
    deprecated: bool = False,
) -> ActionHandler
```

Make a ViewSet method a custom route. `api.viewset()` registers it.

Bolt makes a new instance of the class for each request to a custom action.

**Parameters:**

| Name | Default | Description |
| --- | --- | --- |
| `methods` | required | HTTP methods: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS` or `QUERY`, in any case. |
| `detail` | required | `True`: the route is `{path}/{lookup_field}/{action path}`. `False`: the route is `{path}/{action path}`. |
| `path` | the method name | The last path segment. |
| `name` | the method name | The suffix of the route name, `<base>-<name>`. |
| `auth` | `None` | Authentication backends. Wins over the registration and the class. |
| `guards` | `None` | Guards. Wins over the registration and the class. |
| `response_model` | unset | The response type, in place of the return type. |
| `status_code` | `None` | The success status code. Wins over the registration and the class. |
| `validate_response` | `None` | Turn response validation on or off. |
| `tags` | `None` | OpenAPI tags. Wins over the registration. |
| `summary`, `description` | `None` | OpenAPI fields of the operation. |
| `deprecated` | `False` | Mark the operation as deprecated. |

**Errors:**

- `ValueError` at decoration: a method name that is not in the list above.
- `ValueError` at registration: the class has an action and you register it with `api.view()`.

## BoltAPI.view()

<!-- fragment -->
```python
def view(
    self,
    path: str,
    *,
    name: str | None = None,
    methods: list[str] | None = None,
    guards: list[Any] | None = None,
    auth: list[Any] | None = None,
    status_code: int | None = None,
    validate_response: bool | None = None,
    tags: list[str] | None = None,
    include_in_schema: bool | None = None,
    deprecated: bool = False,
)
```

A class decorator that registers an `APIView` subclass at `path`.

**Parameters:**

| Name | Default | Description |
| --- | --- | --- |
| `path` | required | The route path, with path parameters such as `{article_id}`. |
| `name` | the class name | The route name of each method. |
| `methods` | each method of the class | The HTTP methods to register. |
| `guards`, `auth`, `status_code`, `validate_response`, `include_in_schema` | `None` | Win over the class attributes. |
| `tags` | `None` | OpenAPI tags. |
| `deprecated` | `False` | Mark each operation as deprecated. |

**Errors:**

- `TypeError`: the class is not an `APIView` subclass.
- `ValueError`: the class implements no HTTP method, `methods` is empty, or `methods` names a method that the class does not implement.
- `ValueError`: the class has an `@action` method.

## BoltAPI.viewset()

<!-- fragment -->
```python
def viewset(
    self,
    path: str,
    *,
    name: str | None = None,
    guards: list[Any] | None = None,
    auth: list[Any] | None = None,
    status_code: int | None = None,
    validate_response: bool | None = None,
    lookup_field: str = "pk",
    tags: list[str] | None = None,
    include_in_schema: bool | None = None,
    deprecated: bool = False,
)
```

A class decorator that registers a `ViewSet` subclass. It makes a route for each standard action and each `@action` method of the class.

**Parameters:**

| Name | Default | Description |
| --- | --- | --- |
| `path` | required | The collection path. A trailing slash on `path` goes to each detail and action path too. The `trailing_slash` mode of the API then applies. |
| `name` | the class name | The base of the route names: `<name>-<action>`, for example `ArticleViewSet-list`. |
| `guards`, `auth`, `status_code`, `validate_response`, `tags` | `None` | Apply to each route. An `@action` argument wins. `status_code` also wins over the default status of an action. |
| `lookup_field` | `"pk"` | The path parameter of the detail routes. A value other than `"pk"` wins over the class attribute, for this registration only. |
| `include_in_schema` | `None` | Wins over the class attribute. |
| `deprecated` | `False` | Mark each operation, also each `@action`, as deprecated. |

**Errors:** `TypeError` when the class is not a `ViewSet` subclass.

You can register one class two times, at two paths. Each registration has its own view instances.
