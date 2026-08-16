"""
Tests for pagination with serializer integration.

Verifies that @paginate decorator correctly uses serializers from response_model
or return type annotation for efficient batch serialization.
"""

from __future__ import annotations

import builtins

import msgspec
import pytest

from django_bolt import BoltAPI, CursorPagination, LimitOffsetPagination, PageNumberPagination, paginate
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient
from django_bolt.views import ViewSet

from .test_models import Article, Author, BlogPost

# ============================================================================
# Serializers for Testing
# ============================================================================


class ArticleListSerializer(Serializer):
    """Serializer with subset of fields - should NOT include content or is_published."""

    id: int
    title: str
    author: str


class ArticleDetailSerializer(Serializer):
    """Serializer with all fields."""

    id: int
    title: str
    content: str
    author: str
    is_published: bool


class ArticleMsgspecSchema(msgspec.Struct):
    """Plain msgspec.Struct for testing non-Bolt serializers."""

    id: int
    title: str
    author: str


class AuthorNestedSerializer(Serializer):
    """Nested author serializer for pagination relation tests."""

    id: int
    name: str


class BlogPostListSerializer(Serializer):
    """Blog post serializer with nested author."""

    id: int
    title: str
    author: AuthorNestedSerializer


class ConcurrentBlogPostListSerializer(BlogPostListSerializer):
    """Serializer used to verify page item serialization runs concurrently."""


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def sample_articles(db):
    """Create sample articles in the database."""
    articles = []
    for i in range(1, 26):  # Create 25 articles
        article = Article.objects.create(
            title=f"Article {i}",
            content=f"Content for article {i} - this is detailed content that should not be in list view",
            author=f"Author {i % 5}",
            is_published=i % 2 == 0,
        )
        articles.append(article)
    return articles


@pytest.fixture
def sample_blog_posts(db):
    """Create blog posts with authors for nested pagination tests."""
    posts = []
    for i in range(1, 6):
        author = Author.objects.create(name=f"Author {i}", email=f"author{i}@example.com")
        post = BlogPost.objects.create(
            title=f"Post {i}",
            content=f"Post content {i}",
            author=author,
            published=i % 2 == 0,
        )
        posts.append(post)
    return posts


# ============================================================================
# Tests: Bolt Serializer Integration
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_paginate_with_bolt_serializer_uses_dump_many(sample_articles):
    """Test that @paginate uses Bolt Serializer's dump_many for efficient serialization."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles", response_model=list[ArticleListSerializer])
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        # Page 1
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        assert data["total"] == 25
        assert data["page"] == 1
        assert data["total_pages"] == 3
        assert data["has_next"] is True
        assert data["has_previous"] is False

        # Verify serializer respects field declarations
        first_item = data["items"][0]
        assert "id" in first_item
        assert "title" in first_item
        assert "author" in first_item
        # These fields should NOT be present (not declared in ArticleListSerializer)
        assert "content" not in first_item
        assert "is_published" not in first_item

        # Collect page 1 IDs
        page1_ids = {item["id"] for item in data["items"]}

        # Page 2 - verify different items
        response2 = client.get("/articles?page=2")
        assert response2.status_code == 200
        data2 = response2.json()
        assert len(data2["items"]) == 10
        assert data2["page"] == 2
        assert data2["has_next"] is True
        assert data2["has_previous"] is True

        page2_ids = {item["id"] for item in data2["items"]}
        assert page1_ids.isdisjoint(page2_ids), "Page 2 should have different items than page 1"

        # Page 3 (last page) - verify remaining items
        response3 = client.get("/articles?page=3")
        assert response3.status_code == 200
        data3 = response3.json()
        assert len(data3["items"]) == 5  # Only 5 remaining
        assert data3["page"] == 3
        assert data3["has_next"] is False
        assert data3["has_previous"] is True

        page3_ids = {item["id"] for item in data3["items"]}
        assert page1_ids.isdisjoint(page3_ids), "Page 3 should have different items than page 1"
        assert page2_ids.isdisjoint(page3_ids), "Page 3 should have different items than page 2"

        # Verify all 25 items covered across pages
        all_ids = page1_ids | page2_ids | page3_ids
        assert len(all_ids) == 25


@pytest.mark.django_db(transaction=True)
def test_paginate_with_bolt_serializer_detail_fields(sample_articles):
    """Test pagination with detail serializer includes all declared fields."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 5

    @api.get("/articles", response_model=list[ArticleDetailSerializer])
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 5

        # Verify all declared fields are present
        first_item = data["items"][0]
        assert "id" in first_item
        assert "title" in first_item
        assert "content" in first_item
        assert "author" in first_item
        assert "is_published" in first_item


@pytest.mark.django_db(transaction=True)
def test_paginate_with_return_type_annotation(sample_articles):
    """Test that @paginate works with return type annotation instead of response_model."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request) -> list[ArticleListSerializer]:
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10

        # Verify serializer respects field declarations
        first_item = data["items"][0]
        assert "id" in first_item
        assert "title" in first_item
        assert "author" in first_item
        assert "content" not in first_item
        assert "is_published" not in first_item


@pytest.mark.django_db(transaction=True)
def test_paginate_with_flat_serializer_uses_sync_from_model(sample_articles, monkeypatch):
    """Flat serializers should avoid the async afrom_model() path during pagination."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    async def fail_afrom_model(cls, instance, *, _depth=0, max_depth=10):
        raise AssertionError("flat pagination should not call afrom_model()")

    monkeypatch.setattr(
        ArticleListSerializer,
        "afrom_model",
        classmethod(fail_afrom_model),
    )

    @api.get("/articles", response_model=list[ArticleListSerializer])
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200
        assert len(response.json()["items"]) == 10


@pytest.mark.django_db(transaction=True)
def test_paginate_with_nested_serializer_uses_afrom_model(sample_blog_posts):
    """Test async pagination uses afrom_model for nested relations."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 5

    @api.get("/posts", response_model=list[BlogPostListSerializer])
    @paginate(SmallPagePagination)
    async def list_posts(request):
        return BlogPost.objects.all()

    with TestClient(api) as client:
        response = client.get("/posts?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 5
        first_item = data["items"][0]
        assert first_item["author"]["name"].startswith("Author ")


@pytest.mark.django_db(transaction=True)
def test_paginate_preloads_relations_instead_of_per_item_async_loading(sample_blog_posts, monkeypatch):
    """A page of nested-FK items is served from the serializer's loading plan.

    The relation is select_related before the slice is evaluated, so the
    per-item ``afrom_model()`` fallback (one query per row on the loop) is
    never needed.
    """
    api = BoltAPI()
    seen_ids: list[int] = []
    original_afrom_model = Serializer.afrom_model.__func__

    async def tracked_afrom_model(cls, instance, *, _depth=0, max_depth=10):
        seen_ids.append(instance.pk)
        return await original_afrom_model(cls, instance, _depth=_depth, max_depth=max_depth)

    monkeypatch.setattr(
        ConcurrentBlogPostListSerializer,
        "afrom_model",
        classmethod(tracked_afrom_model),
    )

    class SmallPagePagination(PageNumberPagination):
        page_size = 2

    @api.get("/posts", response_model=list[ConcurrentBlogPostListSerializer])
    @paginate(SmallPagePagination)
    async def list_posts(request):
        return BlogPost.objects.all()

    with TestClient(api) as client:
        response = client.get("/posts?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 2
        assert data["items"][0]["author"]["name"].startswith("Author ")
        assert seen_ids == []


# ============================================================================
# Tests: msgspec.Struct Integration
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_paginate_with_msgspec_struct(sample_articles):
    """Test that @paginate works with plain msgspec.Struct."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles", response_model=list[ArticleMsgspecSchema])
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10

        # Verify msgspec struct respects field declarations
        first_item = data["items"][0]
        assert "id" in first_item
        assert "title" in first_item
        assert "author" in first_item
        # These fields should NOT be present (not declared in ArticleMsgspecSchema)
        assert "content" not in first_item
        assert "is_published" not in first_item


# ============================================================================
# Tests: Backwards Compatibility (No Serializer)
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_paginate_without_serializer_uses_fallback(sample_articles):
    """Test that @paginate falls back to _model_to_dict when no serializer is provided."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    # No response_model or return type annotation
    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10

        # Without serializer, all model fields should be dumped
        first_item = data["items"][0]
        assert "id" in first_item
        assert "title" in first_item
        assert "content" in first_item
        assert "author" in first_item
        assert "is_published" in first_item


# ============================================================================
# Tests: Different Pagination Types with Serializers
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_limit_offset_pagination_with_serializer(sample_articles):
    """Test LimitOffsetPagination with serializer and verify page navigation."""
    api = BoltAPI()

    @api.get("/articles", response_model=list[ArticleListSerializer])
    @paginate(LimitOffsetPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        # First batch (offset=0)
        response1 = client.get("/articles?limit=10&offset=0")
        assert response1.status_code == 200
        data1 = response1.json()
        assert len(data1["items"]) == 10
        assert data1["limit"] == 10
        assert data1["offset"] == 0
        assert data1["total"] == 25
        assert data1["has_next"] is True
        assert data1["has_previous"] is False

        # Verify serializer respects field declarations
        first_item = data1["items"][0]
        assert "id" in first_item
        assert "content" not in first_item

        batch1_ids = {item["id"] for item in data1["items"]}

        # Second batch (offset=10)
        response2 = client.get("/articles?limit=10&offset=10")
        assert response2.status_code == 200
        data2 = response2.json()
        assert len(data2["items"]) == 10
        assert data2["offset"] == 10
        assert data2["has_next"] is True
        assert data2["has_previous"] is True

        batch2_ids = {item["id"] for item in data2["items"]}
        assert batch1_ids.isdisjoint(batch2_ids), "Offset 10 should have different items"

        # Third batch (offset=20) - last batch
        response3 = client.get("/articles?limit=10&offset=20")
        assert response3.status_code == 200
        data3 = response3.json()
        assert len(data3["items"]) == 5  # Only 5 remaining
        assert data3["offset"] == 20
        assert data3["has_next"] is False
        assert data3["has_previous"] is True

        batch3_ids = {item["id"] for item in data3["items"]}
        assert batch1_ids.isdisjoint(batch3_ids)
        assert batch2_ids.isdisjoint(batch3_ids)

        # Verify all 25 items covered
        all_ids = batch1_ids | batch2_ids | batch3_ids
        assert len(all_ids) == 25


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_with_serializer(sample_articles):
    """Test CursorPagination with serializer - navigate through all pages."""
    api = BoltAPI()

    class SmallCursorPagination(CursorPagination):
        page_size = 5
        ordering = "-id"

    @api.get("/articles", response_model=list[ArticleListSerializer])
    @paginate(SmallCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        all_ids = set()
        cursor = None
        page_count = 0

        # Navigate through all pages using cursors
        while True:
            url = "/articles" if cursor is None else f"/articles?cursor={cursor}"
            response = client.get(url)
            assert response.status_code == 200

            data = response.json()
            page_count += 1

            # Verify serializer respects field declarations
            if data["items"]:
                first_item = data["items"][0]
                assert "id" in first_item
                assert "title" in first_item
                assert "author" in first_item
                assert "content" not in first_item

            # Collect IDs and verify no duplicates
            page_ids = {item["id"] for item in data["items"]}
            assert all_ids.isdisjoint(page_ids), f"Page {page_count} has duplicate items"
            all_ids.update(page_ids)

            # Check pagination state
            if page_count == 1:
                assert data["has_previous"] is False
            else:
                assert data["has_previous"] is True

            # Move to next page or exit
            if not data["has_next"]:
                break
            cursor = data["next_cursor"]
            assert cursor is not None

        # Verify we got all 25 items across 5 pages
        assert page_count == 5  # 25 items / 5 per page
        assert len(all_ids) == 25


# ============================================================================
# Tests: Edge Cases
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_paginate_with_empty_queryset_and_serializer(db):
    """Test pagination with empty queryset still works with serializer."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles", response_model=list[ArticleListSerializer])
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 0
        assert data["total"] == 0


@pytest.mark.django_db(transaction=True)
def test_paginate_with_filtered_queryset_and_serializer(sample_articles):
    """Test pagination with filtered queryset and serializer."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles", response_model=list[ArticleListSerializer])
    @paginate(SmallPagePagination)
    async def list_articles(request, is_published: bool = None):
        qs = Article.objects.all()
        if is_published is not None:
            qs = qs.filter(is_published=is_published)
        return qs

    with TestClient(api) as client:
        response = client.get("/articles?is_published=true&page=1")
        assert response.status_code == 200

        data = response.json()
        # Half of 25 articles are published (articles 2, 4, 6, ... 24 = 12 total)
        assert data["total"] == 12
        assert len(data["items"]) == 10

        # Verify serializer still applied
        first_item = data["items"][0]
        assert "content" not in first_item


@pytest.mark.django_db(transaction=True)
def test_paginate_with_values_queryset_uses_fallback(sample_articles):
    """Test that values() querysets fall back to direct dict handling."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles", response_model=list[ArticleListSerializer])
    @paginate(SmallPagePagination)
    async def list_articles(request):
        # values() returns dicts, not model instances
        # The serializer won't be able to use from_model(), so it falls back
        return Article.objects.values("id", "title", "author")

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10

        # values() returns dicts, so all fields from values() will be present
        first_item = data["items"][0]
        assert "id" in first_item
        assert "title" in first_item
        assert "author" in first_item


# ============================================================================
# Tests: ViewSet with Pagination and Serializers
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_viewset_pagination_with_return_type_serializer(sample_articles):
    """Test ViewSet with @paginate decorator and return type annotation serializer."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()

        @paginate(SmallPagePagination)
        async def list(self, request) -> builtins.list[ArticleListSerializer]:
            return await self.get_queryset()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        assert data["total"] == 25
        assert data["page"] == 1

        # Verify serializer respects field declarations
        first_item = data["items"][0]
        assert "id" in first_item
        assert "title" in first_item
        assert "author" in first_item
        # These fields should NOT be present (not declared in ArticleListSerializer)
        assert "content" not in first_item
        assert "is_published" not in first_item

        # Verify page 2 has different items
        response2 = client.get("/articles?page=2")
        assert response2.status_code == 200
        data2 = response2.json()
        assert data2["page"] == 2

        page1_ids = {item["id"] for item in data["items"]}
        page2_ids = {item["id"] for item in data2["items"]}
        assert page1_ids.isdisjoint(page2_ids), "Page 2 should have different items"


@pytest.mark.django_db(transaction=True)
def test_viewset_pagination_with_response_model_serializer(sample_articles):
    """Test ViewSet with @paginate decorator and response_model serializer."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    # Define class first without decorator to avoid name collision
    # Inside class body, 'list' refers to the method, not Python's builtin
    class _ArticleViewSet(ViewSet):
        queryset = Article.objects.all()

        @paginate(SmallPagePagination)
        async def list(self, request):
            return await self.get_queryset()

    # Set response_model AFTER class definition where 'list' is the builtin
    _ArticleViewSet.list.response_model = list[ArticleListSerializer]

    # Now register the viewset
    api.viewset("/articles")(_ArticleViewSet)

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10

        # Verify serializer respects field declarations
        first_item = data["items"][0]
        assert "id" in first_item
        assert "title" in first_item
        assert "author" in first_item
        assert "content" not in first_item
        assert "is_published" not in first_item


@pytest.mark.django_db(transaction=True)
def test_viewset_cursor_pagination_with_serializer(sample_articles):
    """Test ViewSet with CursorPagination and serializer."""
    api = BoltAPI()

    class SmallCursorPagination(CursorPagination):
        page_size = 5
        ordering = "-id"

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()

        @paginate(SmallCursorPagination)
        async def list(self, request) -> list[ArticleListSerializer]:
            return await self.get_queryset()

    with TestClient(api) as client:
        all_ids = set()
        cursor = None
        page_count = 0

        # Navigate through all pages
        while True:
            url = "/articles" if cursor is None else f"/articles?cursor={cursor}"
            response = client.get(url)
            assert response.status_code == 200

            data = response.json()
            page_count += 1

            # Verify serializer on each page
            if data["items"]:
                first_item = data["items"][0]
                assert "content" not in first_item
                assert "is_published" not in first_item

            page_ids = {item["id"] for item in data["items"]}
            assert all_ids.isdisjoint(page_ids), f"Page {page_count} has duplicate items"
            all_ids.update(page_ids)

            if not data["has_next"]:
                break
            cursor = data["next_cursor"]

        assert page_count == 5  # 25 items / 5 per page
        assert len(all_ids) == 25


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
