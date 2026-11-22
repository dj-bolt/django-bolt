---
description: Move an API from Django Ninja, Django REST Framework or FastAPI to Django-Bolt one endpoint at a time, with the old routes still serving.
---

# Migrate from Django Ninja, DRF or FastAPI

You can move an API to Django-Bolt one endpoint at a time. The old API keeps serving the routes that you did not move yet. This guide shows how to run the two APIs side by side, a step-by-step plan, and how the concepts of each framework map to Bolt.

## Run the old API and Bolt side by side

### One server: send the other paths to Django

Bolt routes come first. With `mount_django()`, Bolt sends each request that matches no Bolt route to Django's ASGI app. Django then serves the old Ninja or DRF views from your URLconf:

```python title="myproject/api.py"
from django_bolt import BoltAPI

api = BoltAPI(trailing_slash="append")


@api.get("/api/articles/")
async def list_articles() -> list[dict]:
    return [{"source": "bolt"}]


api.mount_django("/", clear_root_path=True)
```

```bash
curl localhost:8000/api/articles/
```

```json
[{"source": "bolt"}]
```

```bash
curl localhost:8000/api/comments/
```

```json
{"source": "django", "path": "/api/comments/"}
```

Here the URLconf of the project still has the old view for `api/comments/`. `python manage.py runbolt` serves both paths.

- `clear_root_path=True` gives Django the full path, so the URL patterns of the URLconf match as before.
- A Bolt route with the same path as an old view wins. Thus each endpoint that you move takes over its path at once.
- DRF URLs end with a slash. `BoltAPI(trailing_slash="append")` keeps the slash on Bolt routes, so clients keep the same URLs. With the default `"strip"`, Bolt answers `/api/articles/` with a `308` redirect to `/api/articles`.
- Django middleware, authentication and permission classes apply to the old views as before. Bolt auth, guards, rate limits and route `@cors` do not apply to them. The global CORS settings apply.
- `runbolt` mounts the Django admin by itself. You do not mount it.

See [ASGI mounts](asgi-mounts.md) for the details of `mount_django()`.

### Two servers: split the paths at the proxy

You can also keep the old server, for example gunicorn or uvicorn, and run `runbolt` on another port. A reverse proxy then sends the moved paths to Bolt:

```nginx
location /api/v2/ {
    proxy_pass http://127.0.0.1:8000;
}

location / {
    proxy_pass http://127.0.0.1:8001;
}
```

Use this for a FastAPI app. A FastAPI app is not part of the Django project, so `mount_django()` cannot serve it. `api.mount_asgi()` can serve an ASGI app under a prefix. It does not run the lifespan protocol, so the startup code of the app does not run.

## Plan the migration

1. Install Django-Bolt and add `"django_bolt"` to `INSTALLED_APPS`. See [Installation](../intro/install.md).
2. Create `api.py` next to `urls.py`, with `api = BoltAPI()` and a `/health` route.
3. Add `api.mount_django("/", clear_root_path=True)`. Start `python manage.py runbolt`. Run your old test suite against it, or call a few old endpoints. Each answer must be the same as before.
4. Set up authentication in Bolt. Use the same users and tokens as the old API, so a client keeps its credentials. See [Authentication](authentication.md).
5. Pick one read endpoint with few dependencies. Write it in Bolt, at the same path. Write a `TestClient` test that sends the old request and checks the old response.
6. Deploy. Watch the errors and the response times of the moved path.
7. Repeat steps 5 and 6 for each endpoint. Move the write endpoints after the read endpoints.
8. Delete the old views, serializers and URL patterns of the moved endpoints.
9. When the URLconf has no API views, remove `mount_django()`, or keep it for other Django pages.

Keep the JSON shape of each response. A client must not see a change. Check the error responses too: Bolt validation errors are `422` with a `detail` list, and DRF and Ninja use other shapes. Use a custom error handler if clients read the old shape. See [Error handling](error-handling.md).

## From Django Ninja

Django Ninja and Bolt both read typed handler signatures. Most code changes are names.

| Django Ninja | Django-Bolt |
| --- | --- |
| `NinjaAPI()` and `path("api/", api.urls)` | `BoltAPI(prefix="/api")` in `api.py`. `runbolt` finds it. |
| `@api.get(path, response=Out)` | `@api.get(path)` with the return type `-> Out`, or `response_model=Out` |
| `response={201: Out}` and `return 201, obj` | `@api.post(path, status_code=201)` and `return obj` |
| `def handler(request, ...)` | `async def handler(...)`. Add `request: Request` only when you need it. |
| `Schema` (Pydantic) | `msgspec.Struct`, or a Bolt `Serializer` |
| `ModelSchema` with `Meta.model` and `Meta.fields` | `create_serializer(Model, fields=[...])`, or a `Serializer` that you write |
| `Field(..., min_length=3)` | `Annotated[str, Meta(min_length=3)]` |
| `Query`, `Path`, `Header`, `Cookie`, `Form`, `File` | The same names in `django_bolt.param_functions`, in `Annotated[...]` |
| `Router()` and `api.add_router("/articles/", router)` | `Router(prefix="/articles")` and `api.include_router(router)` |
| `auth=django_auth` | `auth=[SessionAuthentication()]` |
| `HttpBearer` subclass with `authenticate()` | `auth=[JWTAuthentication()]` or `auth=[APIKeyAuthentication(...)]` |
| `from ninja.pagination import paginate` | `from django_bolt import paginate` |
| `ninja.testing.TestClient(router)` | `django_bolt.testing.TestClient(api)` |

Django Ninja:

```python
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema

from blog.models import Article

api = NinjaAPI()


class ArticleIn(Schema):
    title: str
    body: str


class ArticleOut(Schema):
    id: int
    title: str


@api.get("/articles/{article_id}", response=ArticleOut)
def get_article(request, article_id: int):
    return get_object_or_404(Article, id=article_id)


@api.post("/articles", response={201: ArticleOut})
def create_article(request, payload: ArticleIn):
    article = Article.objects.create(author=request.user, **payload.dict())
    return 201, article
```

Django-Bolt:

```python
import msgspec

from django_bolt import BoltAPI, CurrentUser
from django_bolt.auth import IsAuthenticated, SessionAuthentication
from django_bolt.exceptions import NotFound

from blog.models import Article

api = BoltAPI()


class ArticleIn(msgspec.Struct):
    title: str
    body: str


class ArticleOut(msgspec.Struct):
    id: int
    title: str


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> ArticleOut:
    try:
        return await Article.objects.aget(id=article_id)
    except Article.DoesNotExist:
        raise NotFound(detail="Not Found")


@api.post(
    "/articles",
    status_code=201,
    auth=[SessionAuthentication()],
    guards=[IsAuthenticated()],
)
async def create_article(data: ArticleIn, user: CurrentUser) -> ArticleOut:
    return await Article.objects.acreate(author=user, **msgspec.structs.asdict(data))
```

```bash
curl localhost:8000/articles/1
```

```json
{"id": 1, "title": "Hello Bolt"}
```

Bolt reads a `msgspec.Struct` response from the attributes of the model instance. A Pydantic `Schema` with validators becomes a Bolt `Serializer` with `@field_validator` and `@model_validator`. See [Serializers](serializers.md).

## From Django REST Framework

DRF reads the request in a serializer. Bolt reads it from the handler signature. A DRF `ModelViewSet` maps to a Bolt `ModelViewSet`.

| Django REST Framework | Django-Bolt |
| --- | --- |
| `ModelSerializer` with `Meta.fields` | A `Serializer` class, or `create_serializer(Model, fields=[...])` |
| `read_only_fields`, `extra_kwargs={"password": {"write_only": True}}` | `field(read_only=True, default=...)`, `field(write_only=True)` |
| `SerializerMethodField` | `@computed_field` |
| `CharField(source="author.email")` | `field(source="author.email")` |
| `validate_<field>()` and `validate()` | `@field_validator("<field>")` and `@model_validator` |
| `serializer.is_valid(raise_exception=True)` | A handler parameter with the serializer type. Bolt answers `422`. |
| `ModelViewSet`, `ReadOnlyModelViewSet`, `APIView` | The same names in `django_bolt` |
| `@action(detail=True, methods=["post"])` | `@action(methods=["POST"], detail=True)` |
| `DefaultRouter().register("articles", ArticleViewSet)` | `@api.viewset("/articles")` on the class |
| `permission_classes = [IsAuthenticated]` | `guards = [IsAuthenticated()]` |
| `IsAdminUser` | `Requires("is_staff", True)` |
| `TokenAuthentication`, `SessionAuthentication` | `JWTAuthentication()`, `APIKeyAuthentication(...)`, `SessionAuthentication()` |
| `pagination_class = PageNumberPagination` | `pagination_class = PageNumberPagination` from `django_bolt` |
| `Response(data, status=201)` | `return data` with `status_code=201`, or `JSON(data, status_code=201)` |
| `APIClient()` | `TestClient(api)` |

Django REST Framework:

```python
from rest_framework import permissions, routers, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from blog.models import Article


class ArticleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Article
        fields = ["id", "title", "body", "author", "published"]
        read_only_fields = ["id"]


class ArticleViewSet(viewsets.ModelViewSet):
    queryset = Article.objects.order_by("id")
    serializer_class = ArticleSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        article = self.get_object()
        article.published = True
        article.save(update_fields=["published"])
        return Response(self.get_serializer(article).data)


router = routers.DefaultRouter()
router.register("articles", ArticleViewSet)
```

Django-Bolt:

```python
from django_bolt import BoltAPI, ModelViewSet, action
from django_bolt.auth import IsAuthenticated, SessionAuthentication
from django_bolt.serializers import Serializer, field

from blog.models import Article

api = BoltAPI(trailing_slash="append")


class ArticleSerializer(Serializer):
    id: int = field(read_only=True, default=0)
    title: str
    body: str
    author_id: int
    published: bool = False


@api.viewset("/articles/")
class ArticleViewSet(ModelViewSet):
    queryset = Article.objects.order_by("id")
    serializer_class = ArticleSerializer
    auth = [SessionAuthentication()]
    guards = [IsAuthenticated()]

    @action(methods=["POST"], detail=True)
    async def publish(self, pk: int) -> ArticleSerializer:
        article = await self.get_object(pk)
        article.published = True
        await article.asave(update_fields=["published"])
        return article
```

The routes are `/articles/`, `/articles/{pk}/` and `/articles/{pk}/publish/`, as with the DRF router.

Some differences to check:

- DRF writes a foreign key field `author` as a primary key. The Bolt `ModelViewSet` passes the fields to the model by name. Name the field `author_id`.
- The DRF list response is `{"count", "next", "previous", "results"}`. The Bolt envelope has `items`, `total` and page fields. Override `build_response()` to keep the DRF shape. See [Pagination](pagination.md#change-the-envelope).
- A DRF permission class runs Python code. A Bolt guard checks a claim of the token, in Rust. Put a check that needs the database in a dependency. See [Permissions](permissions.md).
- DRF `SessionAuthentication` checks a CSRF token. Bolt `SessionAuthentication()` has its own check: it rejects a cross-site request with an unsafe method that sends the session cookie. See [Authentication](authentication.md).
- DRF filter backends have no Bolt form. Override `get_queryset()` or `filter_queryset()`. See [Class-based views](class-based-views.md).

## From FastAPI

FastAPI and Bolt have the same handler style. The main changes are the models and the ORM: Bolt uses msgspec and the Django ORM.

| FastAPI | Django-Bolt |
| --- | --- |
| `FastAPI()` | `BoltAPI()` in a Django project |
| `APIRouter(prefix=..., tags=...)` and `app.include_router(router)` | `Router(prefix=..., tags=...)` and `api.include_router(router)` |
| `BaseModel` | `msgspec.Struct`, or a Bolt `Serializer` |
| `Field(ge=0)` | `Annotated[int, Meta(ge=0)]` |
| `model.model_dump()` | `msgspec.structs.asdict(data)` |
| `Depends(fn)` | `Depends(fn)` from `django_bolt` |
| A dependency with `yield` | Not supported. Return the value, and clean up in the handler. |
| `app.dependency_overrides` | Not supported. See [Dependencies](dependencies.md#limits). |
| `HTTPException(status_code=404, detail="...")` | `HTTPException(status_code=404, detail="...")` from `django_bolt.exceptions` |
| `OAuth2PasswordBearer` with your own JWT code | `JWTAuthentication()` and `create_jwt_for_user()` |
| SQLAlchemy session dependency | The Django ORM. See [Async ORM](async-orm.md). |
| `@app.on_event("startup")`, `lifespan=` | `BoltAPI(lifespan=...)`. See [Lifespan](lifespan.md). |
| `uvicorn main:app` | `python manage.py runbolt` |
| `fastapi.testclient.TestClient(app)` | `django_bolt.testing.TestClient(api)` |

FastAPI:

```python
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI()


class ArticleOut(BaseModel):
    id: int
    title: str


ARTICLES = {1: ArticleOut(id=1, title="Hello Bolt")}


def get_api_version(x_api_version: str = Header("1")) -> str:
    return x_api_version


@app.get("/articles/{article_id}", response_model=ArticleOut)
async def get_article(article_id: int, version: str = Depends(get_api_version)):
    if article_id not in ARTICLES:
        raise HTTPException(status_code=404, detail="Article not found")
    return ARTICLES[article_id]
```

Django-Bolt:

```python
from typing import Annotated

import msgspec

from django_bolt import BoltAPI, Depends
from django_bolt.exceptions import HTTPException
from django_bolt.param_functions import Header

from blog.models import Article

api = BoltAPI()


class ArticleOut(msgspec.Struct):
    id: int
    title: str


def get_api_version(x_api_version: Annotated[str, Header()] = "1") -> str:
    return x_api_version


@api.get("/articles/{article_id}")
async def get_article(article_id: int, version: str = Depends(get_api_version)) -> ArticleOut:
    try:
        return await Article.objects.aget(id=article_id)
    except Article.DoesNotExist:
        raise HTTPException(status_code=404, detail="Article not found")
```

```bash
curl localhost:8000/articles/99
```

```json
{"detail": "Article not found"}
```

A FastAPI project has no Django models. Move the tables to Django models first, with `inspectdb` for an existing database. Then move the routes.

## Port the tests

`TestClient` runs the request pipeline of `runbolt` in the test process. It takes the `BoltAPI` object, not a URL:

```python
from django_bolt.testing import TestClient

from myproject.api import api


def test_list_articles():
    with TestClient(api) as client:
        response = client.get("/api/articles/")
    assert response.status_code == 200
    assert response.json() == [{"source": "bolt"}]
```

The response has `status_code`, `json()`, `text` and `headers`, as in the clients of DRF, Ninja and FastAPI. See [Testing](testing.md).

## See also

- [ASGI mounts](asgi-mounts.md)
- [Serializers](serializers.md)
- [Class-based views](class-based-views.md)
- [Authentication](authentication.md)
- [Comparison with other frameworks](../comparison.md)
