"""
Tests for @action decorator with ViewSets.

This test suite verifies that the @action decorator works correctly:
- Instance-level actions (detail=True)
- Collection-level actions (detail=False)
- Multiple HTTP methods on single action
- Custom path parameter
- Auth/guards inheritance from class-level
"""

import msgspec
import pytest

from django_bolt import BoltAPI, ViewSet, action
from django_bolt.testing import TestClient

from .test_models import Article  # noqa: PLC0415

# --- Fixtures ---


@pytest.fixture
def api():
    """Create a fresh BoltAPI instance for each test."""
    return BoltAPI()


# --- Schemas ---


class ArticleSchema(msgspec.Struct):
    """Article schema."""

    id: int
    title: str
    content: str


class ArticleCreateSchema(msgspec.Struct):
    """Schema for creating articles."""

    title: str
    content: str
    author: str


# --- Tests ---


@pytest.mark.django_db(transaction=True)
def test_action_decorator_detail_true(api):
    """Test @action with detail=True (instance-level action)."""

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def list(self, request):
            """List articles."""
            articles = []
            async for article in await self.get_queryset():
                articles.append(ArticleSchema(id=article.id, title=article.title, content=article.content))
            return articles

        async def retrieve(self, request, pk: int):
            """Retrieve single article."""
            article = await self.get_object(pk)
            return ArticleSchema(id=article.id, title=article.title, content=article.content)

        @action(methods=["POST"], detail=True)
        async def publish(self, request):
            """Publish an article. POST /articles/{pk}/publish"""
            article = await self.get_object()
            article.is_published = True
            await article.asave()
            return {"published": True, "article_id": article.pk}

    # Create test article
    article = Article.objects.create(
        title="Test Article", content="Test content", author="Test Author", is_published=False
    )

    client = TestClient(api)

    # Test the custom action
    response = client.post(f"/articles/{article.id}/publish")
    assert response.status_code == 200
    data = response.json()
    assert data["published"] is True
    assert data["article_id"] == article.id

    # Verify article was published
    article.refresh_from_db()
    assert article.is_published is True


@pytest.mark.django_db(transaction=True)
def test_action_decorator_detail_false(api):
    """Test @action with detail=False (collection-level action)."""

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def list(self, request):
            """List articles."""
            articles = []
            async for article in await self.get_queryset():
                articles.append(ArticleSchema(id=article.id, title=article.title, content=article.content))
            return articles

        @action(methods=["GET"], detail=False)
        async def published(self, request):
            """Get published articles. GET /articles/published"""
            articles = []
            async for article in Article.objects.filter(is_published=True):
                articles.append(ArticleSchema(id=article.id, title=article.title, content=article.content))
            return articles

    # Create test articles
    Article.objects.create(title="Published 1", content="Content 1", author="Author 1", is_published=True)
    Article.objects.create(title="Draft", content="Content 2", author="Author 2", is_published=False)
    Article.objects.create(title="Published 2", content="Content 3", author="Author 3", is_published=True)

    client = TestClient(api)

    # Test the custom action
    response = client.get("/articles/published")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert all(article["title"].startswith("Published") for article in data)


@pytest.mark.django_db(transaction=True)
def test_action_decorator_multiple_methods(api):
    """Test @action with multiple HTTP methods."""

    class StatusUpdate(msgspec.Struct):
        """Schema for status update."""

        is_published: bool

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def list(self, request):
            """List articles."""
            return []

        @action(methods=["GET"], detail=True, path="status")
        async def get_status(self, request):
            """GET /articles/{pk}/status - Get article status"""
            article = await self.get_object()
            return {"is_published": article.is_published}

        @action(methods=["POST"], detail=True, path="status")
        async def update_status(self, request, data: StatusUpdate):
            """POST /articles/{pk}/status - Update article status"""
            article = await self.get_object()
            article.is_published = data.is_published
            await article.asave()
            return {"updated": True, "is_published": article.is_published}

    # Create test article
    article = Article.objects.create(
        title="Test Article", content="Test content", author="Test Author", is_published=False
    )

    client = TestClient(api)

    # Test GET
    response = client.get(f"/articles/{article.id}/status")
    assert response.status_code == 200
    data = response.json()
    assert data["is_published"] is False

    # Test POST
    response = client.post(f"/articles/{article.id}/status", json={"is_published": True})
    assert response.status_code == 200
    data = response.json()
    assert data["updated"] is True
    assert data["is_published"] is True


@pytest.mark.django_db(transaction=True)
def test_action_decorator_custom_path(api):
    """Test @action with custom path parameter."""

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def list(self, request):
            """List articles."""
            return []

        @action(methods=["POST"], detail=True, path="custom-action-name")
        async def some_method_name(self, request, pk: int):
            """POST /articles/{pk}/custom-action-name"""
            return {"action": "custom-action-name", "article_id": pk}

    client = TestClient(api)

    # Create test article
    article = Article.objects.create(title="Test Article", content="Test content", author="Test Author")

    # Test custom path (not method name)
    response = client.post(f"/articles/{article.id}/custom-action-name")
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "custom-action-name"
    assert data["article_id"] == article.id


@pytest.mark.django_db(transaction=True)
def test_action_decorator_with_api_view_raises_error(api):
    """Test that @action raises error when used with api.view() instead of api.viewset()."""

    # This should raise an error because api.view() doesn't support @action
    with pytest.raises(ValueError, match="uses @action decorator.*api.viewset"):

        @api.view("/articles", methods=["GET"])
        class ArticleViewSet(ViewSet):
            async def get(self, request):
                return []

            @action(methods=["POST"], detail=False)
            async def custom_action(self, request):
                return {"ok": True}


@pytest.mark.django_db(transaction=True)
def test_action_decorator_defaults_to_function_name(api):
    """Test that @action uses function name as default path."""

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def list(self, request):
            """List articles."""
            return []

        @action(methods=["POST"], detail=True)
        async def archive(self, request, pk: int):
            """POST /articles/{pk}/archive"""
            return {"archived": True, "article_id": pk}

    client = TestClient(api)

    # Create test article
    article = Article.objects.create(title="Test Article", content="Test content", author="Test Author")

    # Test action at /articles/{pk}/archive (function name)
    response = client.post(f"/articles/{article.id}/archive")
    assert response.status_code == 200
    data = response.json()
    assert data["archived"] is True
    assert data["article_id"] == article.id


@pytest.mark.django_db(transaction=True)
def test_action_decorator_with_query_params(api):
    """Test @action with query parameters."""

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def list(self, request):
            """List articles."""
            return []

        @action(methods=["GET"], detail=False)
        async def search(self, request, query: str, limit: int = 10):
            """GET /articles/search?query=xxx&limit=5"""
            articles = []
            async for article in Article.objects.filter(title__icontains=query)[:limit]:
                articles.append(ArticleSchema(id=article.id, title=article.title, content=article.content))
            return {"query": query, "limit": limit, "results": articles}

    # Create test articles
    Article.objects.create(title="Python Guide", content="Content", author="Author")
    Article.objects.create(title="Django Tutorial", content="Content", author="Author")
    Article.objects.create(title="Python Basics", content="Content", author="Author")

    client = TestClient(api)

    # Test with query params
    response = client.get("/articles/search?query=Python&limit=5")
    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "Python"
    assert data["limit"] == 5
    assert len(data["results"]) == 2


@pytest.mark.django_db(transaction=True)
def test_action_decorator_invalid_method(api):
    """Test that @action raises error for invalid HTTP methods."""

    with pytest.raises(ValueError, match="Invalid HTTP method"):

        @action(methods=["INVALID"], detail=True)
        async def some_action(self, request, pk: int):
            pass


@pytest.mark.django_db(transaction=True)
def test_action_decorator_with_different_lookup_fields(api):
    """Test @action respects custom lookup_field."""

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        lookup_field = "id"  # Explicitly set to 'id' instead of default 'pk'

        async def list(self, request):
            """List articles."""
            return []

        async def retrieve(self, request, id: int):
            """Retrieve article by id."""
            article = await self.get_object(id=id)
            return ArticleSchema(id=article.id, title=article.title, content=article.content)

        @action(methods=["POST"], detail=True)
        async def feature(self, request, id: int):
            """POST /articles/{id}/feature"""
            return {"featured": True, "article_id": id}

    client = TestClient(api)

    # Create test article
    article = Article.objects.create(title="Test Article", content="Test content", author="Test Author")

    # Test action with custom lookup field
    response = client.post(f"/articles/{article.id}/feature")
    assert response.status_code == 200
    data = response.json()
    assert data["featured"] is True
    assert data["article_id"] == article.id
