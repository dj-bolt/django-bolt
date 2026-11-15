"""
Tests for decorator syntax (@api.view and @api.viewset).

This test suite verifies that the decorator pattern works correctly.
"""

import msgspec
import pytest

from django_bolt import BoltAPI, ViewSet, action
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated
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

    with TestClient(api) as client:
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
