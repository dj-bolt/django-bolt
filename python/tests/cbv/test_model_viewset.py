"""
Tests for ModelViewSet and ReadOnlyModelViewSet (DRF-style usage).

This test suite verifies that ModelViewSet and ReadOnlyModelViewSet work similarly
to Django REST Framework's ModelViewSet, where you just set queryset and serializer_class.
"""

import pytest

from django_bolt import BoltAPI, ModelViewSet, ReadOnlyModelViewSet, action
from django_bolt.pagination import PageNumberPagination, paginate
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient
from tests.test_models import Article


@pytest.fixture
def api():
    """Create a fresh BoltAPI instance for each test."""
    return BoltAPI()


# --- Schemas ---


class ArticleSchema(Serializer):
    """Full article schema."""

    id: int
    title: str
    content: str
    author: str
    is_published: bool


class ArticleCreateSchema(Serializer):
    """Schema for creating/updating articles."""

    title: str
    content: str
    author: str


# --- Tests ---


@pytest.mark.django_db(transaction=True)
def test_readonly_model_viewset(api):
    """Test ReadOnlyModelViewSet provides helpers for read operations."""
    # Create test data
    article1 = Article.objects.create(
        title="Article 1",
        content="Content 1",
        author="Author 1",
    )
    Article.objects.create(
        title="Article 2",
        content="Content 2",
        author="Author 2",
    )

    @api.viewset("/articles")
    class ArticleListViewSet(ReadOnlyModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    with TestClient(api) as client:
        # List
        response = client.get("/articles")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert all("id" in article and "title" in article for article in data)

        # Retrieve
        response = client.get(f"/articles/{article1.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == article1.id
        assert data["title"] == "Article 1"


@pytest.mark.django_db(transaction=True)
def test_model_viewset_crud(api):
    """Test ModelViewSet with full CRUD implementation."""

    @api.viewset("/articles")
    class ArticleListViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        create_serializer_class = ArticleCreateSchema

    with TestClient(api) as client:
        # List
        response = client.get("/articles")
        assert response.status_code == 200
        assert response.json() == []

        # Create
        response = client.post(
            "/articles",
            json={"data": {"title": "New Article", "content": "New Content", "author": "Test Author"}},
        )
        assert response.status_code == 201
        article_id = response.json()["id"]

        # Retrieve
        response = client.get(f"/articles/{article_id}")
        assert response.status_code == 200
        assert response.json()["title"] == "New Article"

        # Update
        response = client.put(
            f"/articles/{article_id}",
            json={"data": {"title": "Updated Title", "content": "Updated Content", "author": "Updated Author"}},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "Updated Title"

        # Partial update
        response = client.patch(
            f"/articles/{article_id}",
            json={"data": {"title": "Patched Title"}},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "Patched Title"

        # Delete
        response = client.delete(f"/articles/{article_id}")
        assert response.status_code == 204

        # Verify deletion
        response = client.get(f"/articles/{article_id}")
        assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_model_viewset_lookup_field(api):
    """Test ModelViewSet with custom lookup_field."""
    # Create article
    Article.objects.create(
        title="Test Article",
        content="Content",
        author="test-author",
    )

    @api.viewset("/articles/by-author")
    class ArticleViewSet(ReadOnlyModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        lookup_field = "author"  # Look up by author instead of pk

    with TestClient(api) as client:
        # Lookup by author
        response = client.get("/articles/by-author/test-author")
        assert response.status_code == 200
        data = response.json()
        assert data["author"] == "test-author"
        assert data["title"] == "Test Article"
