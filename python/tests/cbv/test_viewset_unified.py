"""
Tests for unified ViewSet pattern with api.viewset() (Litestar/DRF-inspired).

This test suite verifies that the new unified ViewSet pattern works correctly:
- Single ViewSet for both list and detail views
- DRF-style action methods (list, retrieve, create, update, partial_update, destroy)
- Automatic route generation with api.viewset()
- Different serializers for list vs detail (list_serializer_class)
- Type-driven serialization
"""

import msgspec
import pytest

from django_bolt import BoltAPI, ViewSet, action
from django_bolt.testing import TestClient
from tests.test_models import Article

# --- Schemas ---


class ArticleFullSchema(msgspec.Struct):
    """Full article schema for detail views."""

    id: int
    title: str
    content: str
    author: str
    is_published: bool

    @classmethod
    def from_model(cls, obj):
        return cls(
            id=obj.id,
            title=obj.title,
            content=obj.content,
            author=obj.author,
            is_published=obj.is_published,
        )


class ArticleMiniSchema(msgspec.Struct):
    """Minimal article schema for list views."""

    id: int
    title: str

    @classmethod
    def from_model(cls, obj):
        return cls(id=obj.id, title=obj.title)


class ArticleCreateSchema(msgspec.Struct):
    """Schema for creating articles."""

    title: str
    content: str
    author: str


class ArticleUpdateSchema(msgspec.Struct):
    """Schema for updating articles."""

    title: str | None = None
    content: str | None = None
    author: str | None = None


# --- Tests ---


@pytest.mark.django_db(transaction=True)
def test_unified_viewset_with_custom_actions(api):
    """Test unified ViewSet with custom actions."""

    # Create test data
    Article.objects.create(
        title="Published Article",
        content="Content",
        author="Author",
        is_published=True,
    )
    Article.objects.create(
        title="Draft Article",
        content="Content",
        author="Author",
        is_published=False,
    )

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        """Unified ViewSet with custom actions."""

        queryset = Article.objects.all()
        serializer_class = ArticleFullSchema
        list_serializer_class = ArticleMiniSchema

        async def list(self, request):
            """List all articles."""
            articles = []
            async for article in await self.get_queryset():
                articles.append(ArticleMiniSchema.from_model(article))
            return articles

        # Custom action: search (using @action decorator)
        @action(methods=["GET"], detail=False)
        async def search(self, request, query: str):
            """Search articles by title. GET /articles/search"""
            results = []
            async for article in Article.objects.filter(title__icontains=query):
                results.append(ArticleMiniSchema.from_model(article))
            return {"query": query, "results": results}

        # Custom action: published only (using @action decorator)
        @action(methods=["GET"], detail=False)
        async def published(self, request):
            """Get published articles only. GET /articles/published"""
            articles = []
            async for article in Article.objects.filter(is_published=True):
                articles.append(ArticleMiniSchema.from_model(article))
            return articles

    with TestClient(api) as client:
        # List all articles
        response = client.get("/articles")
        assert response.status_code == 200
        assert len(response.json()) == 2

        # Search
        response = client.get("/articles/search?query=Published")
        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "Published"
        assert len(data["results"]) == 1
        assert data["results"][0]["title"] == "Published Article"

        # Published only
        response = client.get("/articles/published")
        assert response.status_code == 200
        articles = response.json()
        assert len(articles) == 1
        assert articles[0]["title"] == "Published Article"


@pytest.mark.django_db(transaction=True)
def test_unified_viewset_partial_implementation(api):
    """Test unified ViewSet with only some actions implemented."""

    @api.viewset("/articles")
    class ReadOnlyArticleViewSet(ViewSet):
        """Read-only ViewSet (only list and retrieve)."""

        queryset = Article.objects.all()
        serializer_class = ArticleFullSchema

        async def list(self, request):
            """List articles."""
            articles = []
            async for article in await self.get_queryset():
                articles.append(ArticleFullSchema.from_model(article))
            return articles

        async def retrieve(self, request, pk: int):
            """Retrieve a single article."""
            article = await self.get_object(pk)
            return ArticleFullSchema.from_model(article)

        # Note: create, update, partial_update, destroy not implemented

    with TestClient(api) as client:
        # List works
        response = client.get("/articles")
        assert response.status_code == 200

        # POST not registered (create not implemented)
        response = client.post("/articles", json={"title": "Test", "content": "Test", "author": "Test"})
        assert response.status_code == 404


@pytest.fixture
def api():
    """Create a fresh BoltAPI instance for each test."""
    return BoltAPI()
