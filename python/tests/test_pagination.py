"""
Real ORM-based tests for pagination functionality.

Tests pagination with actual Django models, database queries, and HTTP requests.
No mocking - tests the full integration stack.
"""

import base64
from datetime import UTC, datetime
from typing import Generic, TypeVar
from urllib.parse import quote

import msgspec
import pytest

from django_bolt import (
    BoltAPI,
    CursorPagination,
    LimitOffsetPagination,
    ListMixin,
    ModelViewSet,
    PageNumberPagination,
    Request,
    ViewSet,
    paginate,
)
from django_bolt.testing import TestClient

from .test_models import Article, Author, BlogPost, Release, User, UserProfile

# ============================================================================
# Schemas
# ============================================================================


class ArticleSchema(msgspec.Struct):
    id: int
    title: str
    content: str
    author: str
    is_published: bool


class ArticleListSchema(msgspec.Struct):
    id: int
    title: str
    author: str


T = TypeVar("T")


class PathPage(msgspec.Struct, Generic[T]):  # noqa: UP046 — PEP 695 params are unresolvable under PEP 563
    """Envelope that shows the request path. Only build_response() can fill it."""

    path: str
    results: list[T]


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def sample_articles(db):
    """Create sample articles in the database"""
    articles = []
    for i in range(1, 51):  # Create 50 articles
        article = Article.objects.create(
            title=f"Article {i}",
            content=f"Content for article {i}",
            author=f"Author {i % 10}",
            is_published=i % 2 == 0,  # Half published, half not
        )
        articles.append(article)
    return articles


# ============================================================================
# PageNumberPagination Tests
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_page_number_pagination_first_page(sample_articles):
    """Test PageNumberPagination first page with real ORM"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert "items" in data
        assert "total" in data
        assert len(data["items"]) == 10
        assert data["total"] == 50
        assert data["page"] == 1
        assert data["page_size"] == 10
        assert data["total_pages"] == 5
        assert data["has_next"] is True
        assert data["has_previous"] is False
        assert data["next_page"] == 2
        assert data.get("previous_page") is None

        # Check first item (ordered by -created_at)
        assert data["items"][0]["title"] == "Article 50"


@pytest.mark.django_db(transaction=True)
def test_page_number_pagination_middle_page(sample_articles):
    """Test pagination on middle page"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=3")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 3
        assert len(data["items"]) == 10
        assert data["has_next"] is True
        assert data["has_previous"] is True
        assert data["next_page"] == 4
        assert data["previous_page"] == 2


@pytest.mark.django_db(transaction=True)
def test_page_number_pagination_last_page(sample_articles):
    """Test pagination on last page"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=5")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 5
        assert len(data["items"]) == 10
        assert data["has_next"] is False
        assert data["has_previous"] is True
        assert data.get("next_page") is None
        assert data["previous_page"] == 4


@pytest.mark.django_db(transaction=True)
def test_page_number_pagination_custom_page_size(sample_articles):
    """Test pagination with custom page size via query param"""
    api = BoltAPI()

    class CustomPagination(PageNumberPagination):
        page_size = 10
        page_size_query_param = "page_size"

    @api.get("/articles")
    @paginate(CustomPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1&page_size=20")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 20
        assert data["page_size"] == 20
        assert data["total_pages"] == 3


@pytest.mark.django_db(transaction=True)
def test_page_number_pagination_empty_results(db):
    """Test pagination with no results"""
    api = BoltAPI()

    @api.get("/articles")
    @paginate(PageNumberPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 0
        assert data["total"] == 0
        assert data["total_pages"] == 0
        assert data["has_next"] is False
        assert data["has_previous"] is False


@pytest.mark.django_db(transaction=True)
def test_pagination_page_or_offset_beyond_the_data(db):
    """A page or an offset after the last row gives an empty page. A very large number is not a server error."""
    for number in range(7):
        Article.objects.create(title=f"Article {number}", content="content", author="author")
    api = BoltAPI()

    @api.get("/none/page")
    @paginate(PageNumberPagination)
    async def page_of_no_rows(request):
        return Article.objects.filter(author="nobody")

    @api.get("/none-list/page")
    @paginate(PageNumberPagination)
    async def page_of_empty_list(request):
        return []

    @api.get("/all/offset")
    @paginate(LimitOffsetPagination)
    async def offset_of_rows(request):
        return Article.objects.all()

    @api.get("/all-list/offset")
    @paginate(LimitOffsetPagination)
    async def offset_of_list(request):
        return [{"id": number} for number in range(7)]

    with TestClient(api) as client:
        # With no rows, each page number is page 1. There is no page before it.
        for path in ("/none/page", "/none-list/page"):
            first_page = client.get(path).json()
            assert first_page == {
                "items": [],
                "total": 0,
                "has_next": False,
                "has_previous": False,
                "page": 1,
                "page_size": 100,
                "total_pages": 0,
            }
            assert client.get(f"{path}?page=3").json() == first_page
            # The row offset of this page does not fit the integer of the database.
            assert client.get(f"{path}?page={2**64}").json() == first_page

        # An offset after the last row gives no items. The response shows the offset of the request.
        for path in ("/all/offset", "/all-list/offset"):
            for offset in (100, 2**63):
                response = client.get(f"{path}?limit=2&offset={offset}")
                assert response.status_code == 200, response.text
                assert response.json() == {
                    "items": [],
                    "total": 7,
                    "has_next": False,
                    "has_previous": True,
                    "limit": 2,
                    "offset": offset,
                }


@pytest.mark.django_db(transaction=True)
def test_page_number_pagination_with_filtering(sample_articles):
    """Test pagination with queryset filtering"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
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
        assert data["total"] == 25  # Half are published
        assert len(data["items"]) == 10


@pytest.mark.django_db(transaction=True)
def test_page_number_pagination_with_ordering(sample_articles):
    """Test pagination respects queryset ordering"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.order_by("title")

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        # Alphabetical ordering: Article 1, Article 10, Article 11, ..., Article 19, Article 2, ...
        assert data["items"][0]["title"] == "Article 1"
        assert data["items"][1]["title"] == "Article 10"


@pytest.mark.django_db(transaction=True)
def test_paginate_accepts_a_sync_handler(sample_articles):
    """A sync handler under @paginate gives the same page as an async handler."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/sync")
    @paginate(SmallPagePagination)
    def list_sync(request):
        return Article.objects.order_by("id")

    @api.get("/async")
    @paginate(SmallPagePagination)
    async def list_async(request):
        return Article.objects.order_by("id")

    with TestClient(api) as client:
        response = client.get("/sync?page=2")
        assert response.status_code == 200

        data = response.json()
        assert (data["page"], data["total"]) == (2, 50)
        assert [item["title"] for item in data["items"]] == [f"Article {i}" for i in range(11, 21)]
        assert data == client.get("/async?page=2").json()


# ============================================================================
# LimitOffsetPagination Tests
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_limit_offset_pagination_basic(sample_articles):
    """Test LimitOffsetPagination with real ORM"""
    api = BoltAPI()

    @api.get("/articles")
    @paginate(LimitOffsetPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?limit=10&offset=0")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        assert data["total"] == 50
        assert data["limit"] == 10
        assert data["offset"] == 0
        assert data["has_next"] is True
        assert data["has_previous"] is False


@pytest.mark.django_db(transaction=True)
def test_limit_offset_pagination_with_offset(sample_articles):
    """Test limit-offset with specific offset"""
    api = BoltAPI()

    @api.get("/articles")
    @paginate(LimitOffsetPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?limit=10&offset=20")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        assert data["limit"] == 10
        assert data["offset"] == 20
        assert data["has_next"] is True
        assert data["has_previous"] is True


@pytest.mark.django_db(transaction=True)
def test_limit_offset_pagination_last_page(sample_articles):
    """Test limit-offset on last page"""
    api = BoltAPI()

    @api.get("/articles")
    @paginate(LimitOffsetPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?limit=10&offset=45")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 5  # Only 5 items left
        assert data["has_next"] is False
        assert data["has_previous"] is True


@pytest.mark.django_db(transaction=True)
def test_limit_offset_pagination_default_limit(sample_articles):
    """Test default limit when not specified"""
    api = BoltAPI()

    class CustomLimitOffset(LimitOffsetPagination):
        page_size = 15

    @api.get("/articles")
    @paginate(CustomLimitOffset)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 15
        assert data["limit"] == 15
        assert data["offset"] == 0


@pytest.mark.django_db(transaction=True)
def test_limit_offset_pagination_max_limit_enforcement(sample_articles):
    """Test that max_page_size is enforced"""
    api = BoltAPI()

    class LimitedOffsetPagination(LimitOffsetPagination):
        page_size = 10
        max_page_size = 25

    @api.get("/articles")
    @paginate(LimitedOffsetPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?limit=100&offset=0")
        assert response.status_code == 200

        data = response.json()
        assert data["limit"] == 25  # Clamped to max
        assert len(data["items"]) == 25


@pytest.mark.django_db(transaction=True)
def test_limit_offset_pagination_with_filtering(sample_articles):
    """Test limit-offset pagination with filtering"""
    api = BoltAPI()

    @api.get("/articles")
    @paginate(LimitOffsetPagination)
    async def list_articles(request, author: str = None):
        qs = Article.objects.all()
        if author:
            qs = qs.filter(author=author)
        return qs

    with TestClient(api) as client:
        response = client.get("/articles?limit=10&offset=0&author=Author 1")
        assert response.status_code == 200

        data = response.json()
        # Author 1 appears at indices: 1, 11, 21, 31, 41 (5 total)
        assert data["total"] == 5
        assert len(data["items"]) == 5
        assert all(item["author"] == "Author 1" for item in data["items"])


@pytest.mark.django_db(transaction=True)
def test_limit_offset_pagination_with_ordering(sample_articles):
    """Test limit-offset respects queryset ordering"""
    api = BoltAPI()

    @api.get("/articles")
    @paginate(LimitOffsetPagination)
    async def list_articles(request):
        return Article.objects.order_by("id")

    with TestClient(api) as client:
        response = client.get("/articles?limit=5&offset=0")
        assert response.status_code == 200

        data = response.json()
        # Should get Article 1-5 in ascending ID order
        assert data["items"][0]["title"] == "Article 1"
        assert data["items"][4]["title"] == "Article 5"


@pytest.mark.django_db(transaction=True)
def test_limit_offset_pagination_empty_results(db):
    """Test limit-offset with no results"""
    api = BoltAPI()

    @api.get("/articles")
    @paginate(LimitOffsetPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?limit=10&offset=0")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 0
        assert data["total"] == 0
        assert data["has_next"] is False
        assert data["has_previous"] is False


@pytest.mark.django_db(transaction=True)
def test_limit_offset_pagination_offset_beyond_total(sample_articles):
    """Test offset beyond total results"""
    api = BoltAPI()

    @api.get("/articles")
    @paginate(LimitOffsetPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?limit=10&offset=100")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 0
        assert data["total"] == 50
        assert data["has_next"] is False
        assert data["has_previous"] is True


# ============================================================================
# CursorPagination Tests
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_first_page(sample_articles):
    """Test cursor pagination first page"""
    api = BoltAPI()

    class SmallCursorPagination(CursorPagination):
        page_size = 10
        ordering = "-id"

    @api.get("/articles")
    @paginate(SmallCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        assert data["page_size"] == 10
        assert data["has_next"] is True
        assert data["has_previous"] is False
        assert data["next_cursor"] is not None


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_with_cursor(sample_articles):
    """Test cursor pagination with cursor value"""
    api = BoltAPI()

    class SmallCursorPagination(CursorPagination):
        page_size = 10
        ordering = "-id"

    @api.get("/articles")
    @paginate(SmallCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        # Get first page
        response1 = client.get("/articles")
        assert response1.status_code == 200
        data1 = response1.json()
        next_cursor = data1["next_cursor"]

        # Get second page using cursor
        response2 = client.get(f"/articles?cursor={next_cursor}")
        assert response2.status_code == 200

        data2 = response2.json()
        assert len(data2["items"]) == 10
        assert data2["has_previous"] is True

        # Ensure items are different
        items1_ids = {item["id"] for item in data1["items"]}
        items2_ids = {item["id"] for item in data2["items"]}
        assert items1_ids.isdisjoint(items2_ids)


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_ascending_order(sample_articles):
    """Test cursor pagination with ascending order"""
    api = BoltAPI()

    class AscendingCursorPagination(CursorPagination):
        page_size = 10
        ordering = "id"  # Ascending

    @api.get("/articles")
    @paginate(AscendingCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        # First item should have lowest ID
        assert data["items"][0]["title"] == "Article 1"


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_last_page(sample_articles):
    """Test cursor pagination on last page"""
    api = BoltAPI()

    class SmallCursorPagination(CursorPagination):
        page_size = 10
        ordering = "-id"

    @api.get("/articles")
    @paginate(SmallCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        # Navigate through pages to get to last
        cursor = None
        page_count = 0

        while True:
            url = "/articles" if cursor is None else f"/articles?cursor={cursor}"
            response = client.get(url)
            assert response.status_code == 200

            data = response.json()
            page_count += 1

            if not data["has_next"]:
                # Last page
                assert data["has_previous"] is True
                assert data.get("next_cursor") is None
                break

            cursor = data["next_cursor"]

            # Safety check to prevent infinite loop
            if page_count > 10:
                break

        assert page_count == 5  # 50 items / 10 per page


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_with_filtering(sample_articles):
    """Test cursor pagination with filtering"""
    api = BoltAPI()

    class SmallCursorPagination(CursorPagination):
        page_size = 3
        ordering = "-id"

    @api.get("/articles")
    @paginate(SmallCursorPagination)
    async def list_articles(request, is_published: bool = None):
        qs = Article.objects.all()
        if is_published is not None:
            qs = qs.filter(is_published=is_published)
        return qs

    with TestClient(api) as client:
        response = client.get("/articles?is_published=true")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 3
        assert all(item["is_published"] for item in data["items"])


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_custom_page_size(sample_articles):
    """Test cursor pagination with custom page size"""
    api = BoltAPI()

    class CustomCursorPagination(CursorPagination):
        page_size = 10
        page_size_query_param = "page_size"
        max_page_size = 30
        ordering = "-id"

    @api.get("/articles")
    @paginate(CustomCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page_size=20")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 20
        assert data["page_size"] == 20


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_max_page_size_enforcement(sample_articles):
    """Test cursor pagination enforces max page size"""
    api = BoltAPI()

    class LimitedCursorPagination(CursorPagination):
        page_size = 10
        page_size_query_param = "page_size"
        max_page_size = 15
        ordering = "-id"

    @api.get("/articles")
    @paginate(LimitedCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page_size=50")
        assert response.status_code == 200

        data = response.json()
        assert data["page_size"] == 15  # Clamped to max
        assert len(data["items"]) == 15


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_empty_results(db):
    """Test cursor pagination with no results"""
    api = BoltAPI()

    class SmallCursorPagination(CursorPagination):
        page_size = 10
        ordering = "-id"

    @api.get("/articles")
    @paginate(SmallCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 0
        assert data["has_next"] is False
        assert data["has_previous"] is False
        assert data.get("next_cursor") is None


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_ordering_by_title(sample_articles):
    """Test cursor pagination with custom ordering field"""
    api = BoltAPI()

    class TitleOrderedPagination(CursorPagination):
        page_size = 10
        ordering = "title"  # Order by title

    @api.get("/articles")
    @paginate(TitleOrderedPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        # First item should be "Article 1" (alphabetically first)
        assert data["items"][0]["title"] == "Article 1"


def _walk_cursor_pages(client, path, key="id"):
    """Follow next_cursor from the first page to the last page. Return the item IDs in order."""
    ids = []
    url = path
    for _ in range(100):
        response = client.get(url)
        assert response.status_code == 200, response.content
        data = response.json()
        ids.extend(item[key] for item in data["items"])
        cursor = data.get("next_cursor")
        if cursor is None:
            return ids
        url = f"{path}?cursor={quote(cursor)}"
    raise AssertionError("cursor pagination did not reach a last page")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ordering", ["author", "-author"])
def test_cursor_pagination_keeps_rows_that_share_the_ordering_value(sample_articles, ordering):
    """A page that ends inside a group of equal ordering values must not skip the rest of the group."""
    api = BoltAPI()

    class AuthorCursorPagination(CursorPagination):
        page_size = 3  # 5 articles per author, so pages end inside a group
        ordering = "author"

    AuthorCursorPagination.ordering = ordering

    @api.get("/articles")
    @paginate(AuthorCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        ids = _walk_cursor_pages(client, "/articles")

    expected = list(
        Article.objects.order_by(ordering, "-pk" if ordering.startswith("-") else "pk").values_list("pk", flat=True)
    )
    assert ids == expected


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_keeps_rows_with_equal_datetimes(sample_articles):
    """Rows created in the same instant page through "-created_at" without gaps."""
    Article.objects.filter(pk__lte=25).update(created_at=datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=UTC))
    Article.objects.filter(pk__gt=25).update(created_at=datetime(2026, 1, 2, tzinfo=UTC))
    api = BoltAPI()

    class CreatedCursorPagination(CursorPagination):
        page_size = 7
        ordering = "-created_at"

    @api.get("/articles")
    @paginate(CreatedCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        ids = _walk_cursor_pages(client, "/articles")

    assert ids == list(Article.objects.order_by("-created_at", "-pk").values_list("pk", flat=True))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("pk_name", ["id", "pk"])
def test_cursor_pagination_keeps_tied_rows_with_values_queryset(sample_articles, pk_name):
    """A values() queryset that includes the primary key pages through tied values without gaps.

    The primary key can have its column name or the name "pk".
    """
    api = BoltAPI()

    class AuthorCursorPagination(CursorPagination):
        page_size = 3
        ordering = "author"

    @api.get("/articles")
    @paginate(AuthorCursorPagination)
    async def list_articles(request):
        return Article.objects.values(pk_name, "title", "author")

    with TestClient(api) as client:
        ids = _walk_cursor_pages(client, "/articles", key=pk_name)

    assert ids == list(Article.objects.order_by("author", "pk").values_list("pk", flat=True))


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_values_queryset_without_primary_key_is_an_error(sample_articles, settings):
    """With no primary key in values(), the next cursor cannot break ties, so the page fails loudly."""
    settings.DEBUG = True  # the 500 body then shows the exception
    api = BoltAPI()

    class AuthorCursorPagination(CursorPagination):
        page_size = 3
        ordering = "author"

    @api.get("/articles")
    @paginate(AuthorCursorPagination)
    async def list_articles(request):
        return Article.objects.values("title", "author")

    with TestClient(api) as client:
        response = client.get("/articles")

    assert response.status_code == 500
    assert "ValueError" in response.text
    assert "needs the primary key" in response.text


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_keeps_tied_rows_with_a_composite_primary_key(db):
    """A composite primary key breaks ties with all of its columns."""
    for product in ("api", "cli", "web"):
        for version in range(1, 5):
            Release.objects.create(product=product, version=version, channel="stable" if version % 2 else "beta")
    api = BoltAPI()

    class ChannelCursorPagination(CursorPagination):
        page_size = 4
        ordering = "channel"

    @api.get("/release-models")
    @paginate(ChannelCursorPagination)
    async def list_release_models(request):
        return Release.objects.all()

    expected = [f"{r.product}:{r.version}" for r in Release.objects.order_by("channel", "pk")]
    legacy_cursor = base64.b64encode(b'{"v":"beta"}').decode()
    with TestClient(api) as client:
        pages = []
        url = "/release-models"
        for _ in range(len(expected)):  # A cursor that does not move must not hang the test.
            data = client.get(url).json()
            pages.extend(f"{item['product']}:{item['version']}" for item in data["items"])
            if not data.get("next_cursor"):
                break
            url = f"/release-models?cursor={quote(data['next_cursor'])}"

        # A cursor from before the tie-breaker holds no primary key. It continues after its value.
        legacy_page = client.get(f"/release-models?cursor={quote(legacy_cursor)}").json()

    assert pages == expected
    assert [f"{item['product']}:{item['version']}" for item in legacy_page["items"]] == expected[6:10]


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_get_page_params_override_may_omit_cursor_pk(sample_articles):
    """An override of get_page_params from before the tie-breaker returns no cursor_pk. Pages still load."""
    api = BoltAPI()

    class LegacyCursorPagination(CursorPagination):
        page_size = 3
        ordering = "author"

        async def get_page_params(self, request):
            params = await super().get_page_params(request)
            return {"cursor": params["cursor"], "page_size": params["page_size"]}

    @api.get("/articles")
    @paginate(LegacyCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 3


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_accepts_a_cursor_without_a_primary_key(sample_articles):
    """A cursor issued before the tie-breaker existed holds only the ordering value. It still works."""
    api = BoltAPI()

    class AuthorCursorPagination(CursorPagination):
        page_size = 3
        ordering = "author"

    @api.get("/articles")
    @paginate(AuthorCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    legacy_cursor = base64.b64encode(b'{"v":"Author 3"}').decode()
    with TestClient(api) as client:
        response = client.get(f"/articles?cursor={quote(legacy_cursor)}")

    assert response.status_code == 200
    assert [item["author"] for item in response.json()["items"]] == ["Author 4"] * 3


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_cursor_needs_no_percent_encoding(db):
    """A client can put next_cursor into the query string as it is."""
    # In the standard base64 alphabet, the cursors of these titles have "+" and "/".
    # A "+" in a query string is a space, so such a cursor did not decode.
    for title in ("ab>", "ab?", "ac"):
        Article.objects.create(title=title, content="content", author="author")
    api = BoltAPI()

    class TitleCursorPagination(CursorPagination):
        page_size = 1
        ordering = "title"

    @api.get("/articles")
    @paginate(TitleCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        titles = []
        url = "/articles"
        for _ in range(4):
            data = client.get(url).json()
            titles.extend(item["title"] for item in data["items"])
            if "next_cursor" not in data:
                break
            url = f"/articles?cursor={data['next_cursor']}"
        assert titles == ["ab>", "ab?", "ac"]

        # A client can hold a cursor in the standard alphabet from an earlier release.
        first_pk = Article.objects.get(title="ab>").pk
        legacy_cursor = base64.b64encode(b'{"v":"ab>","pk":%d}' % first_pk).decode()
        assert "+" in legacy_cursor
        response = client.get(f"/articles?cursor={quote(legacy_cursor)}")
        assert [item["title"] for item in response.json()["items"]] == ["ab?"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("path", "cursor_json"),
    [
        ("/by-id", b'{"v":"x"}'),
        ("/by-id", b'{"v":[1]}'),
        ("/by-id", b'"x"'),
        ("/by-id", b"[1]"),
        ("/by-author", b'{"v":"Author 1","pk":"x"}'),
        ("/by-author", b'{"v":"Author 1","pk":[1]}'),
        ("/by-channel", b'{"v":"stable","pk":["api","x"]}'),
        ("/by-channel", b'{"v":"stable","pk":["api"]}'),
    ],
)
def test_cursor_pagination_cursor_with_a_wrong_value_gives_the_first_page(sample_articles, path, cursor_json):
    """A cursor that decodes to a value of the wrong type is no cursor. It is not a server error."""
    for version in range(1, 5):
        Release.objects.create(product="api", version=version, channel="stable")
    api = BoltAPI()

    class IdCursorPagination(CursorPagination):
        page_size = 3
        ordering = "-id"

    class AuthorCursorPagination(CursorPagination):
        page_size = 3
        ordering = "author"

    class ChannelCursorPagination(CursorPagination):
        page_size = 3
        ordering = "channel"

    @api.get("/by-id")
    @paginate(IdCursorPagination)
    async def list_by_id(request):
        return Article.objects.all()

    @api.get("/by-author")
    @paginate(AuthorCursorPagination)
    async def list_by_author(request):
        return Article.objects.all()

    @api.get("/by-channel")
    @paginate(ChannelCursorPagination)
    async def list_by_channel(request):
        return Release.objects.all()  # a composite primary key

    cursor = base64.urlsafe_b64encode(cursor_json).decode()
    with TestClient(api) as client:
        first_page = client.get(path)
        response = client.get(f"{path}?cursor={cursor}")

    assert first_page.status_code == 200
    assert len(first_page.json()["items"]) == 3
    assert response.status_code == 200, response.text
    assert response.json() == first_page.json()


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_multiple_pages_continuity(sample_articles):
    """Test that cursor pagination maintains continuity across pages"""
    api = BoltAPI()

    class SmallCursorPagination(CursorPagination):
        page_size = 10
        ordering = "-id"

    @api.get("/articles")
    @paginate(SmallCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        # Get first page
        response1 = client.get("/articles")
        data1 = response1.json()

        # Get second page
        response2 = client.get(f"/articles?cursor={data1['next_cursor']}")
        data2 = response2.json()

        # Get third page
        response3 = client.get(f"/articles?cursor={data2['next_cursor']}")
        data3 = response3.json()

        # Collect all IDs
        all_ids = []
        all_ids.extend([item["id"] for item in data1["items"]])
        all_ids.extend([item["id"] for item in data2["items"]])
        all_ids.extend([item["id"] for item in data3["items"]])

        # Ensure no duplicates
        assert len(all_ids) == len(set(all_ids))

        # Ensure ordering is maintained (descending IDs)
        assert all_ids == sorted(all_ids, reverse=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("ordering", "key"),
    [("-pk", "id"), ("pk", "id"), ("-id", "pk")],
)
def test_cursor_pagination_finds_the_primary_key_of_a_values_row_by_each_name(sample_articles, ordering, key):
    """An ordering by the primary key reads it from a values() row as "pk" or as the column name."""
    api = BoltAPI()

    class PkCursorPagination(CursorPagination):
        page_size = 20

    PkCursorPagination.ordering = ordering

    @api.get("/articles")
    @paginate(PkCursorPagination)
    async def list_articles(request):
        return Article.objects.values(key, "title")

    ids = []
    url = "/articles"
    with TestClient(api) as client:
        for _ in range(3):
            response = client.get(url)
            assert response.status_code == 200, response.content
            data = response.json()
            ids.extend(item[key] for item in data["items"])
            url = f"/articles?cursor={quote(data.get('next_cursor') or '')}"

    assert ids == list(Article.objects.order_by(ordering).values_list("pk", flat=True))


# ============================================================================
# ViewSet Integration Tests
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_viewset_with_pagination(sample_articles):
    """Test ViewSet with @paginate decorator"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    # Use @paginate decorator on ViewSet method
    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        @paginate(SmallPagePagination)
        async def list(self, request):
            return await self.get_queryset()

    with TestClient(api) as client:
        response = client.get("/articles?page=2")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 2
        assert len(data["items"]) == 10
        assert data["total"] == 50


@pytest.mark.django_db(transaction=True)
def test_viewset_with_limit_offset_pagination(sample_articles):
    """Test ViewSet with LimitOffsetPagination"""
    api = BoltAPI()

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        @paginate(LimitOffsetPagination)
        async def list(self, request):
            return await self.get_queryset()

    with TestClient(api) as client:
        response = client.get("/articles?limit=15&offset=10")
        assert response.status_code == 200

        data = response.json()
        assert data["limit"] == 15
        assert data["offset"] == 10
        assert len(data["items"]) == 15


@pytest.mark.django_db(transaction=True)
def test_viewset_with_cursor_pagination(sample_articles):
    """Test ViewSet with CursorPagination"""
    api = BoltAPI()

    class SmallCursorPagination(CursorPagination):
        page_size = 10
        ordering = "-id"

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        @paginate(SmallCursorPagination)
        async def list(self, request):
            return await self.get_queryset()

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        assert data["has_next"] is True
        assert data["next_cursor"] is not None


@pytest.mark.django_db(transaction=True)
def test_viewset_without_pagination(sample_articles):
    """Test ViewSet without pagination returns all results"""
    api = BoltAPI()

    @api.viewset("/articles")
    class ArticleViewSet(ListMixin, ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        pagination_class = None

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 50


@pytest.mark.django_db(transaction=True)
def test_modelviewset_basic_list(sample_articles):
    """Test ModelViewSet basic list operation (pagination can be added manually)"""
    api = BoltAPI()

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        list_serializer_class = ArticleListSchema

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 50  # All articles returned (no pagination)


# ============================================================================
# Edge Cases and Validation
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_pagination_max_page_size_enforcement(sample_articles):
    """Test that max_page_size is enforced"""
    api = BoltAPI()

    class LimitedPagination(PageNumberPagination):
        page_size = 10
        max_page_size = 25
        page_size_query_param = "page_size"

    @api.get("/articles")
    @paginate(LimitedPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1&page_size=100")
        assert response.status_code == 200

        data = response.json()
        assert data["page_size"] == 25  # Clamped to max
        assert len(data["items"]) == 25


@pytest.mark.django_db(transaction=True)
def test_pagination_invalid_page_defaults_to_first(sample_articles):
    """Test that invalid page number defaults to page 1"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=invalid")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 1


@pytest.mark.django_db(transaction=True)
def test_pagination_page_exceeds_total_clamps_to_last(sample_articles):
    """Test that page number exceeding total pages clamps to last page"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=999")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 5  # Last page
        assert len(data["items"]) == 10


@pytest.mark.django_db(transaction=True)
def test_limit_offset_negative_offset_defaults_to_zero(sample_articles):
    """Test that negative offset defaults to 0"""
    api = BoltAPI()

    @api.get("/articles")
    @paginate(LimitOffsetPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?limit=10&offset=-5")
        assert response.status_code == 200

        data = response.json()
        assert data["offset"] == 0


@pytest.mark.django_db(transaction=True)
def test_pagination_zero_page_size_uses_default(sample_articles):
    """Test that zero page size uses default"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10
        page_size_query_param = "page_size"

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1&page_size=0")
        assert response.status_code == 200

        data = response.json()
        assert data["page_size"] == 10  # Default


@pytest.mark.django_db(transaction=True)
def test_pagination_boundary_values_of_the_query_parameters(db):
    """The smallest and the last valid values work. Values that are not valid give the defaults."""
    for number in range(1, 6):
        Article.objects.create(title=f"a{number}", content="c", author="x", is_published=True)
    api = BoltAPI()

    class PagePagination(PageNumberPagination):
        page_size = 2
        max_page_size = 5

    class OffsetPagination(LimitOffsetPagination):
        page_size = 2
        max_page_size = 5

    @api.get("/page")
    @paginate(PagePagination)
    async def list_by_page(request):
        return Article.objects.order_by("id")

    @api.get("/offset")
    @paginate(OffsetPagination)
    async def list_by_offset(request):
        return Article.objects.order_by("id")

    page_1 = {"titles": ["a1", "a2"], "page": 1, "page_size": 2, "has_previous": False, "previous_page": None}
    first_2 = {"titles": ["a1", "a2"], "limit": 2, "offset": 0, "has_previous": False}
    cases = {
        # Page number: a page below 1 is page 1.
        "/page?page=0": page_1,
        "/page?page=-3": page_1,
        "/page?page=2": {"titles": ["a3", "a4"], "page": 2, "previous_page": 1, "next_page": 3},
        "/page?page=3": {"titles": ["a5"], "page": 3, "has_next": False, "next_page": None},
        # A page after the only page is that page.
        "/page?page=9&page_size=5": {"titles": ["a1", "a2", "a3", "a4", "a5"], "page": 1, "total_pages": 1},
        "/page?page_size=1": {"titles": ["a1"], "page_size": 1, "total_pages": 5},
        "/page?page_size=-1": page_1,
        "/page?page_size=many": page_1,
        # Limit and offset.
        "/offset?limit=1": {"titles": ["a1"], "limit": 1, "has_next": True},
        "/offset?limit=-1": first_2,
        "/offset?limit=many": first_2,
        "/offset?offset=far": first_2,
        "/offset?offset=1": {"titles": ["a2", "a3"], "offset": 1, "has_previous": True},
        "/offset?limit=2&offset=2": {"titles": ["a3", "a4"], "has_next": True},
        # The page ends with the last row, so there is no next page.
        "/offset?limit=2&offset=3": {"titles": ["a4", "a5"], "has_next": False},
    }

    with TestClient(api) as client:
        for url, expected in cases.items():
            response = client.get(url)
            assert response.status_code == 200, url
            data = response.json()
            actual = {key: data.get(key) for key in expected if key != "titles"}
            actual["titles"] = [item["title"] for item in data["items"]]
            assert actual == expected, url


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("scheme", [PageNumberPagination, LimitOffsetPagination, CursorPagination])
def test_build_response_gets_the_request(db, scheme):
    """Each scheme gives the request to build_response(), so a custom envelope can make links."""
    Article.objects.create(title="a1", content="c", author="x", is_published=True)
    api = BoltAPI()

    class PathPagination(scheme):
        response_class = PathPage

        def build_response(self, items, *, total, request, has_next, has_previous, **info):
            return PathPage(path=request.path, results=items)

    @api.get("/articles", response_model=list[ArticleListSchema])
    @paginate(PathPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles")

    assert response.status_code == 200
    data = response.json()
    assert data["path"] == "/articles"
    assert [item["title"] for item in data["results"]] == ["a1"]


def test_pagination_of_a_list():
    """A handler can return a list. The page is a slice of the list."""
    rows = [{"id": number, "name": f"row {number}"} for number in range(1, 8)]
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 3

    @api.get("/by-page")
    @paginate(SmallPagePagination)
    async def list_by_page(request):
        return rows

    @api.get("/by-offset")
    @paginate(LimitOffsetPagination)
    async def list_by_offset(request):
        return rows

    with TestClient(api) as client:
        by_page = client.get("/by-page?page=3")
        by_offset = client.get("/by-offset?limit=2&offset=4")

    assert by_page.status_code == 200
    assert by_page.json() == {
        "items": [{"id": 7, "name": "row 7"}],
        "total": 7,
        "has_next": False,
        "has_previous": True,
        "page": 3,
        "page_size": 3,
        "total_pages": 3,
        "previous_page": 2,
    }
    assert by_offset.status_code == 200
    assert by_offset.json() == {
        "items": [{"id": 5, "name": "row 5"}, {"id": 6, "name": "row 6"}],
        "total": 7,
        "has_next": True,
        "has_previous": True,
        "limit": 2,
        "offset": 4,
    }


@pytest.mark.django_db(transaction=True)
def test_paginate_reads_the_request_parameter_of_the_handler(sample_articles):
    """The request can come after another parameter, and it can have a name other than "request"."""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 2

    @api.get("/request-after-a-parameter")
    @paginate(SmallPagePagination)
    async def request_after_a_parameter(author: str, request):
        return Article.objects.filter(author=author).order_by("id")

    @api.get("/request-with-another-name")
    @paginate(SmallPagePagination)
    async def request_with_another_name(req: Request):
        return Article.objects.order_by("id")

    with TestClient(api) as client:
        after_a_parameter = client.get("/request-after-a-parameter?author=Author 1&page=2")
        another_name = client.get("/request-with-another-name?page=2")

    assert after_a_parameter.status_code == 200
    assert after_a_parameter.json()["page"] == 2
    assert [item["title"] for item in after_a_parameter.json()["items"]] == ["Article 21", "Article 31"]
    assert another_name.status_code == 200
    assert another_name.json()["page"] == 2
    assert [item["title"] for item in another_name.json()["items"]] == ["Article 3", "Article 4"]


@pytest.mark.django_db(transaction=True)
def test_pagination_with_query_optimization(sample_articles):
    """Test pagination works with .only() query optimization"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.only("id", "title", "author")

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        assert "title" in data["items"][0]
        assert "author" in data["items"][0]


# ============================================================================
# Advanced Integration & Edge Cases
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_pagination_with_select_related():
    """Test pagination works with select_related"""
    author = Author.objects.create(name="Ada", email="ada@example.com")
    for i in range(12):
        BlogPost.objects.create(title=f"Post {i}", content="body", author=author)
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/posts")
    @paginate(SmallPagePagination)
    async def list_posts(request):
        return BlogPost.objects.select_related("author")

    with TestClient(api) as client:
        response = client.get("/posts?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        assert data["total"] == 12


@pytest.mark.django_db(transaction=True)
def test_pagination_with_prefetch_related(sample_articles):
    """Test pagination works with prefetch_related"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        # Even though Article doesn't have M2M in this test, this tests compatibility
        return Article.objects.all().prefetch_related()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10


@pytest.mark.django_db(transaction=True)
def test_pagination_with_distinct(sample_articles):
    """Test pagination works with distinct"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all().distinct()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10


@pytest.mark.django_db(transaction=True)
def test_pagination_with_values(sample_articles):
    """Test pagination works with values()"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.values("id", "title", "author")

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        # values() returns dicts
        assert isinstance(data["items"][0], dict)
        assert "title" in data["items"][0]


@pytest.mark.django_db(transaction=True)
def test_pagination_with_values_list(sample_articles):
    """Test pagination works with values_list()"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.values_list("id", "title", "author")

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10
        # values_list() returns tuples (serialized as lists)
        assert isinstance(data["items"][0], list)


@pytest.mark.django_db(transaction=True)
def test_pagination_single_result(db):
    """Test pagination with only one result"""
    api = BoltAPI()

    # Create single article
    Article.objects.create(
        title="Solo Article",
        content="Content",
        author="Author",
        is_published=True,
    )

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["total_pages"] == 1
        assert data["has_next"] is False
        assert data["has_previous"] is False


@pytest.mark.django_db(transaction=True)
def test_limit_offset_with_zero_limit(sample_articles):
    """Test limit offset with zero limit uses default"""
    api = BoltAPI()

    class CustomLimitOffset(LimitOffsetPagination):
        page_size = 20

    @api.get("/articles")
    @paginate(CustomLimitOffset)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?limit=0&offset=0")
        assert response.status_code == 200

        data = response.json()
        assert data["limit"] == 20  # Uses default
        assert len(data["items"]) == 20


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_invalid_cursor(sample_articles):
    """Test cursor pagination with invalid cursor value"""
    api = BoltAPI()

    class SmallCursorPagination(CursorPagination):
        page_size = 10
        ordering = "-id"

    @api.get("/articles")
    @paginate(SmallCursorPagination)
    async def list_articles(request):
        return Article.objects.all()

    with TestClient(api) as client:
        # Use invalid cursor - should start from beginning
        response = client.get("/articles?cursor=invalid_cursor_value")
        assert response.status_code == 200

        data = response.json()
        # Invalid cursor should be ignored and start from beginning
        assert len(data["items"]) == 10


@pytest.mark.django_db(transaction=True)
def test_multiple_pagination_types_different_endpoints(sample_articles):
    """Test using different pagination types on different endpoints"""
    api = BoltAPI()

    class PagePagination(PageNumberPagination):
        page_size = 10

    class OffsetPagination(LimitOffsetPagination):
        page_size = 15

    class CursorPag(CursorPagination):
        page_size = 5
        ordering = "-id"

    @api.get("/articles/page")
    @paginate(PagePagination)
    async def list_page(request):
        return Article.objects.all()

    @api.get("/articles/offset")
    @paginate(OffsetPagination)
    async def list_offset(request):
        return Article.objects.all()

    @api.get("/articles/cursor")
    @paginate(CursorPag)
    async def list_cursor(request):
        return Article.objects.all()

    with TestClient(api) as client:
        # Test page pagination
        response1 = client.get("/articles/page?page=1")
        assert response1.status_code == 200
        data1 = response1.json()
        assert len(data1["items"]) == 10
        assert "page" in data1

        # Test limit-offset pagination (no limit specified, uses page_size default)
        response2 = client.get("/articles/offset")
        assert response2.status_code == 200
        data2 = response2.json()
        assert len(data2["items"]) == 15
        assert "limit" in data2

        # Test cursor pagination
        response3 = client.get("/articles/cursor")
        assert response3.status_code == 200
        data3 = response3.json()
        assert len(data3["items"]) == 5
        assert "next_cursor" in data3


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_after_a_null_ordering_value_is_an_error(settings):
    """A null value cannot make a cursor. The page fails loudly, not with has_next and no cursor."""
    settings.DEBUG = True  # the 500 body then shows the exception
    for number in range(4):
        user = User.objects.create(username=f"u{number}", email=f"u{number}@example.com", password_hash="x")
        UserProfile.objects.create(user=user, date_of_birth=None)
    api = BoltAPI()

    class BirthdayCursorPagination(CursorPagination):
        page_size = 2
        ordering = "date_of_birth"

    @api.get("/profiles")
    @paginate(BirthdayCursorPagination)
    async def list_profiles(request):
        return UserProfile.objects.all()

    with TestClient(api) as client:
        response = client.get("/profiles")

    assert response.status_code == 500
    assert "ValueError" in response.text
    assert "is null" in response.text


@pytest.mark.django_db(transaction=True)
def test_cursor_pagination_values_queryset_without_the_ordering_field_is_an_error(sample_articles, settings):
    settings.DEBUG = True  # the 500 body then shows the exception
    api = BoltAPI()

    class TitleCursorPagination(CursorPagination):
        page_size = 3
        ordering = "-id"

    @api.get("/articles")
    @paginate(TitleCursorPagination)
    async def list_articles(request):
        return Article.objects.values("title")

    with TestClient(api) as client:
        response = client.get("/articles")

    assert response.status_code == 500
    assert "ValueError" in response.text
    assert "in values() to make the next cursor" in response.text
