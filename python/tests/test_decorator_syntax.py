"""
Tests for decorator syntax (@api.view and @api.viewset).

This test suite verifies that the decorator pattern works correctly.
"""

import functools

import msgspec
import pytest

from django_bolt import BoltAPI, ViewSet, action
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
