"""
Tests for decorator syntax (@api.view and @api.viewset).

This test suite verifies that the decorator pattern works correctly.
"""

import msgspec
import pytest

from django_bolt import BoltAPI, Router, ViewSet, action
from django_bolt.middleware import TimingMiddleware, cors, middleware, no_compress, rate_limit, skip_middleware
from django_bolt.pagination import PageNumberPagination, paginate
from django_bolt.testing import TestClient
from django_bolt.views import APIView

from .test_models import Article  # noqa: PLC0415

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
