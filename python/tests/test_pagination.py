"""
Real ORM-based tests for pagination functionality.

Tests pagination with actual Django models, database queries, and HTTP requests.
No mocking - tests the full integration stack.
"""

import msgspec
import pytest

from django_bolt import (
    BoltAPI,
    CursorPagination,
    LimitOffsetPagination,
    ListMixin,
    ModelViewSet,
    PageNumberPagination,
    ViewSet,
    paginate,
)
from django_bolt.testing import TestClient

from .test_models import Article

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
def test_pagination_with_select_related(sample_articles):
    """Test pagination works with select_related"""
    api = BoltAPI()

    class SmallPagePagination(PageNumberPagination):
        page_size = 10

    @api.get("/articles")
    @paginate(SmallPagePagination)
    async def list_articles(request):
        # Even though Article doesn't have FK in this test, this tests compatibility
        return Article.objects.all().select_related()

    with TestClient(api) as client:
        response = client.get("/articles?page=1")
        assert response.status_code == 200

        data = response.json()
        assert len(data["items"]) == 10


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
