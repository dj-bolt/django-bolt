"""
Django ORM Integration Tests for Class-Based Views.

This test suite verifies that ViewSets and Mixins work correctly with
real Django ORM operations (like Django REST Framework).

Tests cover:
- Real database queries with Django async ORM
- PartialUpdateMixin with partial updates + asave()
- DestroyMixin with obj.adelete()
- End-to-end CRUD workflows
"""

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.testing import TestClient
from django_bolt.views import (
    APIView,
)
from tests.test_models import Article

# --- Fixtures ---


@pytest.fixture
def api():
    """Create a fresh BoltAPI instance for each test."""
    return BoltAPI()


@pytest.fixture
def sample_articles(db):
    """Create sample articles in the database."""
    from asgiref.sync import async_to_sync  # noqa: PLC0415

    articles = []
    for i in range(1, 4):
        article = async_to_sync(Article.objects.acreate)(
            title=f"Article {i}",
            content=f"Content {i}",
            author="Test Author",
            is_published=(i % 2 == 0),
        )
        articles.append(article)
    return articles


# --- Schemas ---


class ArticleSchema(msgspec.Struct):
    """Full article schema (without datetime fields for simplicity)."""

    id: int
    title: str
    content: str
    author: str
    is_published: bool

    @classmethod
    def from_model(cls, obj):
        """Convert Django model instance to schema."""
        return cls(
            id=obj.id,
            title=obj.title,
            content=obj.content,
            author=obj.author,
            is_published=obj.is_published,
        )


class ArticleCreateSchema(msgspec.Struct):
    """Schema for creating articles."""

    title: str
    content: str
    author: str


class ArticleUpdateSchema(msgspec.Struct):
    """Schema for updating articles (full update)."""

    title: str
    content: str
    author: str
    is_published: bool


class ArticlePartialUpdateSchema(msgspec.Struct):
    """Schema for partial updates (all fields optional)."""

    title: str | None = None
    content: str | None = None
    author: str | None = None
    is_published: bool | None = None


# --- Edge Cases ---


@pytest.mark.django_db(transaction=True)
def test_async_queryset_iteration(api, sample_articles):
    """Test that async queryset iteration works correctly."""

    @api.view("/articles")
    class ArticleListView(APIView):
        async def get(self, request) -> list:
            articles = []
            # Test async iteration like ListMixin does
            async for article in Article.objects.all():
                articles.append(
                    {
                        "id": article.id,
                        "title": article.title,
                    }
                )
            return articles

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        assert all("id" in article and "title" in article for article in data)
