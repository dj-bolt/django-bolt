"""
Tests for decorator syntax (@api.view and @api.viewset).

This test suite verifies that the decorator pattern works correctly.
"""

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
