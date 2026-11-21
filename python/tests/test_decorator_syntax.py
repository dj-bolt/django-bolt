"""
Tests for decorator syntax (@api.view and @api.viewset).

This test suite verifies that the decorator pattern works correctly.
"""

import functools

import msgspec
import pytest

from django_bolt import BoltAPI, Router, ViewSet, action
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated
from django_bolt.middleware import TimingMiddleware, cors, middleware, no_compress, rate_limit, skip_middleware
from django_bolt.pagination import PageNumberPagination, paginate
from django_bolt.responses import PlainText
from django_bolt.testing import TestClient
from django_bolt.views import APIView

from .test_models import Article  # noqa: PLC0415


class _Thing(msgspec.Struct):
    id: int


# --- Fixtures ---


@pytest.fixture
def api():
    """Create a fresh BoltAPI instance for each test."""
    return BoltAPI()


# --- Tests ---


def test_view_decorator_syntax(api):
    """Test @api.view() decorator syntax."""

    @api.view("/health")
    class HealthView(APIView):
        async def get(self, request):
            return {"status": "healthy"}

    client = TestClient(api)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


async def _demo_function_middleware(request, call_next):
    return await call_next(request)


@pytest.mark.parametrize(
    ("name", "config_decorator"),
    [
        ("rate_limit", rate_limit(rps=1, burst=1)),
        ("cors", cors(origins=["https://example.com"])),
        ("skip_middleware", skip_middleware("compression")),
        ("skip_middleware", no_compress),
        ("middleware", middleware(TimingMiddleware)),
        ("middleware", middleware(_demo_function_middleware)),
        ("paginate", paginate(PageNumberPagination)),
    ],
)
def test_a_config_decorator_above_the_route_decorator_raises(api, name, config_decorator):
    """The route decorator reads the configuration, so a decorator above it has no effect."""

    @config_decorator
    @api.get("/items")
    async def handler(request):
        return {"ok": True}

    with pytest.raises(TypeError, match=rf"@{name} is above the route decorator of GET /items, so it has no effect"):
        TestClient(api)


def test_a_config_decorator_above_the_websocket_decorator_raises(api):
    @rate_limit(rps=1, burst=1)
    @api.websocket("/ws")
    async def handler(websocket):
        await websocket.accept()

    with pytest.raises(
        TypeError, match="@rate_limit is above the route decorator of WEBSOCKET /ws, so it has no effect"
    ):
        TestClient(api)


def test_a_config_decorator_below_the_route_decorator_applies(api):
    @api.get("/limited")
    @rate_limit(rps=1, burst=1)
    async def handler():
        return {"ok": True}

    with TestClient(api) as client:
        assert [client.get("/limited").status_code for _ in range(3)] == [200, 429, 429]


def test_a_config_decorator_between_two_route_decorators_applies_to_the_upper_route(api):
    """One function can serve two routes. The upper route decorator reads the configuration."""

    @api.get("/limited")
    @rate_limit(rps=1, burst=1)
    @api.get("/free")
    async def handler():
        return {"ok": True}

    with TestClient(api) as client:
        assert [client.get("/free").status_code for _ in range(3)] == [200, 200, 200]
        assert [client.get("/limited").status_code for _ in range(3)] == [200, 429, 429]


def test_a_config_decorator_above_a_router_decorator_applies(api):
    """A router registers its routes in include_router(), after each decorator ran."""
    router = Router()

    @rate_limit(rps=1, burst=1)
    @router.get("/limited")
    async def handler():
        return {"ok": True}

    api.include_router(router)
    with TestClient(api) as client:
        assert [client.get("/limited").status_code for _ in range(3)] == [200, 429, 429]


def test_view_decorator_with_multiple_methods(api):
    """Test @api.view() with multiple HTTP methods."""

    class ItemData(msgspec.Struct):
        name: str

    @api.view("/items")
    class ItemView(APIView):
        async def get(self, request):
            return {"items": ["item1", "item2"]}

        async def post(self, request, data: ItemData):
            return {"created": True, "item": data.name}

    client = TestClient(api)

    response = client.get("/items")
    assert response.status_code == 200
    assert "items" in response.json()

    response = client.post("/items", json={"name": "test"})
    assert response.status_code == 200
    assert response.json()["created"] is True


class _AsyncCallable:
    async def __call__(self, q: int = 0):
        """Answer with the query value."""
        return {"kind": "async", "q": q}


class _SyncCallable:
    def __call__(self, q: int = 0):
        return {"kind": "sync", "q": q}


@pytest.mark.parametrize(("handler", "kind"), [(_AsyncCallable(), "async"), (_SyncCallable(), "sync")])
def test_an_object_with_call_is_a_route_handler(api, handler, kind):
    """The route registers the bound __call__ method, and the decorator returns the object."""
    assert api.get("/call")(handler) is handler

    with TestClient(api) as client:
        response = client.get("/call?q=5")
        assert response.status_code == 200, response.text
        assert response.json() == {"kind": kind, "q": 5}
        assert client.get("/call?q=x").status_code == 422


def test_a_partial_as_route_handler_raises_a_clear_error(api):
    async def handler(request, k: int = 1):
        return {"k": k}

    with pytest.raises(TypeError, match="A route handler cannot be a functools.partial"):
        api.get("/partial")(functools.partial(handler, k=2))


def test_a_route_handler_that_is_not_callable_raises_a_clear_error(api):
    with pytest.raises(TypeError, match="A route handler must be callable, not str 'handler'"):
        api.get("/text")("handler")


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "head", "options", "query"])
def test_method_decorator_passes_its_arguments_to_the_route(api, method):
    """Each method decorator gives ``status_code`` and the OpenAPI arguments to its route."""

    @getattr(api, method)(
        "/things", status_code=202, tags=["stock"], summary="Short text", description="Long text", name="things"
    )
    async def list_things():
        return {"ok": True}

    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.request(method.upper(), "/things")
        assert response.status_code == 202
        # A HEAD response has no body.
        if method != "head":
            assert response.json() == {"ok": True}

        operation = client.get("/docs/openapi.json").json()["paths"]["/things"][method]

    assert operation["tags"] == ["stock"]
    assert operation["summary"] == "Short text"
    assert operation["description"] == "Long text"
    assert list(operation["responses"]) == ["202"]
    assert api._handler_meta[api._routes[0][2]]["name"] == "things"


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "head", "options", "query"])
def test_method_decorator_passes_auth_and_response_arguments(api, method):
    """Each method decorator gives ``auth``, ``guards`` and the response arguments to its route."""
    route = getattr(api, method)

    @route(
        "/guarded",
        auth=[APIKeyAuthentication(api_keys={"secret"})],
        guards=[IsAuthenticated()],
        response_model=_Thing,
    )
    async def guarded() -> dict:
        return {"id": "not a number"}

    @route("/open", validate_response=False, include_in_schema=False)
    async def open_things() -> _Thing:
        return {"id": "not a number"}

    @route("/text", response_class=PlainText)
    async def text():
        return "plain"

    api._register_openapi_routes()

    with TestClient(api, raise_server_exceptions=False) as client:
        assert client.request(method.upper(), "/guarded").status_code == 401
        # The body is not a _Thing, so the response_model rejects it.
        assert client.request(method.upper(), "/guarded", headers={"X-API-Key": "secret"}).status_code == 500
        # With validation on, this body gives 500 too.
        assert client.request(method.upper(), "/open").status_code == 200

        paths = client.get("/docs/openapi.json").json()["paths"]

    assert "/open" not in paths
    assert list(paths["/text"][method]["responses"]["200"]["content"]) == ["text/plain"]


@pytest.mark.django_db(transaction=True)
def test_viewset_decorator_syntax(api):
    """Test @api.viewset() decorator syntax."""

    class ArticleSchema(msgspec.Struct):
        id: int
        title: str

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def list(self, request):
            return []

        async def retrieve(self, request):
            article = await self.get_object()
            return ArticleSchema(id=article.id, title=article.title)

    # Create test article
    article = Article.objects.create(title="Test Article", content="Test content", author="Test Author")

    client = TestClient(api)

    # Test list
    response = client.get("/articles")
    assert response.status_code == 200

    # Test retrieve
    response = client.get(f"/articles/{article.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Test Article"


def _item_viewset():
    class Items(ViewSet):
        async def list(self, request):
            return {"action": "list"}

        async def retrieve(self, request, pk: int):
            return {"action": "retrieve", "pk": pk}

        @action(methods=["GET"], detail=True)
        async def detail_action(self, request, pk: int):
            return {"action": "detail", "pk": pk}

        @action(methods=["GET"], detail=False)
        async def collection_action(self, request):
            return {"action": "collection"}

    return Items


@pytest.mark.parametrize(
    ("trailing_slash", "routes"),
    [
        ("strip", {"/items", "/items/{pk}", "/items/{pk}/detail_action", "/items/collection_action"}),
        ("append", {"/items/", "/items/{pk}/", "/items/{pk}/detail_action/", "/items/collection_action/"}),
        # "keep" does no normalization, so each route keeps the slash of the viewset path.
        ("keep", {"/items/", "/items/{pk}/", "/items/{pk}/detail_action/", "/items/collection_action/"}),
    ],
)
def test_a_viewset_path_with_a_trailing_slash_gives_paths_with_single_slashes(trailing_slash, routes):
    api = BoltAPI(trailing_slash=trailing_slash)
    api.viewset("/items/")(_item_viewset())

    assert {path for _method, path, _id, _handler in api._routes} == routes
    with TestClient(api) as client:
        slash = "" if trailing_slash == "strip" else "/"
        assert client.get(f"/items{slash}").json() == {"action": "list"}
        assert client.get(f"/items/7{slash}").json() == {"action": "retrieve", "pk": 7}
        assert client.get(f"/items/7/detail_action{slash}").json() == {"action": "detail", "pk": 7}
        assert client.get(f"/items/collection_action{slash}").json() == {"action": "collection"}


def test_a_viewset_path_with_no_trailing_slash_keeps_its_paths():
    api = BoltAPI(trailing_slash="keep")
    api.viewset("/items")(_item_viewset())

    assert {path for _method, path, _id, _handler in api._routes} == {
        "/items",
        "/items/{pk}",
        "/items/{pk}/detail_action",
        "/items/collection_action",
    }


def test_view_with_an_empty_methods_list_raises(api):
    """An empty list registered no route, with no error."""

    class Health(APIView):
        async def get(self, request):
            return {"status": "healthy"}

    with pytest.raises(ValueError, match="methods of @api.view\\(\\) is empty"):
        api.view("/health", methods=[])(Health)
    assert api._routes == []


@pytest.mark.django_db(transaction=True)
def test_viewset_decorator_with_custom_actions(api):
    """Test @api.viewset() decorator with @action decorator."""

    class ArticleSchema(msgspec.Struct):
        id: int
        title: str

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def list(self, request):
            return []

        @action(methods=["POST"], detail=True)
        async def publish(self, request):
            article = await self.get_object()
            article.is_published = True
            await article.asave()
            return {"published": True, "article_id": article.pk}

        @action(methods=["GET"], detail=False)
        async def published(self, request):
            articles = []
            async for article in Article.objects.filter(is_published=True):
                articles.append(ArticleSchema(id=article.id, title=article.title))
            return articles

    # Create test articles
    Article.objects.create(title="Published", content="Content", author="Author", is_published=True)
    article2 = Article.objects.create(title="Draft", content="Content", author="Author", is_published=False)

    client = TestClient(api)

    # Test custom action: publish
    response = client.post(f"/articles/{article2.id}/publish")
    assert response.status_code == 200
    assert response.json()["published"] is True

    # Test custom action: published
    response = client.get("/articles/published")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2  # Both should be published now
