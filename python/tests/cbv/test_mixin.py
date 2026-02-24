import pytest

from django_bolt import BoltAPI, ModelViewSet, ViewSet
from django_bolt.pagination import PageNumberPagination, paginate
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient
from django_bolt.views import (
    CreateMixin,
    DestroyMixin,
    ListMixin,
    PartialUpdateMixin,
    ReadOnlyModelViewSet,
    RetrieveMixin,
    UpdateMixin,
)
from tests.test_models import Article


@pytest.fixture
def api():
    return BoltAPI()


class ArticleSchema(Serializer):
    id: int
    title: str
    content: str
    author: str
    is_published: bool


class ArticleListSchema(Serializer):
    id: int
    title: str

class ArticleCreateSchema(Serializer):
    title: str
    content: str
    author: str


@pytest.fixture
def sample_articles(db):
    """Create sample articles in the database"""
    articles = []
    for i in range(1, 46):  # Create 45 articles
        article = Article.objects.create(
            title=f"Article {i}",
            content=f"Content for article {i}",
            author=f"Author {i % 10}",
            is_published=i % 2 == 0,  # Half published, half not
        )
        articles.append(article)
    return articles


# --- Tests ---


@pytest.mark.parametrize(
    "view_classes",
    [
        (ListMixin, ViewSet),
        (ReadOnlyModelViewSet,),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_list_mixin(api, view_classes: list[type]):
    # Create test data
    Article.objects.create(title="Title 1", content="Content 1", author="Author 1")
    Article.objects.create(title="Title 2", content="Content 2", author="Author 2")

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

        titles = [item["title"] for item in data]
        assert "Title 1" in titles
        assert "Title 2" in titles


@pytest.mark.parametrize("view_classes", [(ListMixin, ViewSet), (ReadOnlyModelViewSet,), (ModelViewSet,)])
@pytest.mark.django_db(transaction=True)
def test_list_mixin_pagination(api, view_classes: list[type]):
    # Create test data
    for i in range(15):
        Article.objects.create(title=f"Title {i}", content=f"Content {i}", author="Author")

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all().order_by("id")
        serializer_class = ArticleSchema
        pagination_class = PageNumberPagination

    with TestClient(api) as client:
        # Test first page
        response = client.get("/articles?page=1&page_size=10")
        assert response.status_code == 200
        data = response.json()

        assert "items" in data
        assert "total" in data
        assert data["total"] == 15
        assert len(data["items"]) == 10
        assert data["page_size"] == 10
        assert data["has_next"] is True
        assert data["has_previous"] is False

        # Test second page
        response = client.get("/articles?page=2&page_size=10")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 5
        assert data["has_next"] is False
        assert data["has_previous"] is True


@pytest.mark.parametrize(
    "view_classes",
    [
        (ListMixin, ViewSet),
        (ReadOnlyModelViewSet,),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_list_mixin_filtering(api, view_classes: list[type]):
    # Create test data
    Article.objects.create(title="Title 1", content="Content 1", author="Author 1", is_published=True)
    Article.objects.create(title="Title 2", content="Content 2", author="Author 2", is_published=False)

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def filter_queryset(self, queryset):
            is_published = self.request.query.get("is_published")
            if is_published == "1":
                queryset = queryset.filter(is_published=True)
            elif is_published == "0":
                queryset = queryset.filter(is_published=False)
            return queryset

    with TestClient(api) as client:
        # Test filtering for published
        response = client.get("/articles?is_published=1")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["title"] == "Title 1"

        # Test filtering for draft
        response = client.get("/articles?is_published=0")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["title"] == "Title 2"


@pytest.mark.parametrize(
    "view_classes",
    [
        (RetrieveMixin, ViewSet),
        (ReadOnlyModelViewSet,),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_retrieve_mixin(api, view_classes: list[type]):
    # Create test data
    article = Article.objects.create(title="Retrieve Title", content="Retrieve Content", author="Author 1")

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    with TestClient(api) as client:
        response = client.get(f"/articles/{article.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == article.id
        assert data["title"] == "Retrieve Title"
        assert data["content"] == "Retrieve Content"




@pytest.mark.parametrize(
    "view_classes",
    [
        (CreateMixin, ViewSet),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_create_mixin(api, view_classes: list[type]):
    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        create_serializer_class = ArticleCreateSchema

    with TestClient(api) as client:
        response = client.post(
            "/articles",
            json={"data": {"title": "Create Title", "content": "Create Content", "author": "Author 1"}},
        )
        assert response.status_code == 201
        data = response.json()
        assert "id" in data
        assert data["title"] == "Create Title"
        assert Article.objects.filter(id=data["id"]).exists()


@pytest.mark.parametrize(
    "view_classes",
    [
        (UpdateMixin, ViewSet),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_update_mixin(api, view_classes: list[type]):
    article = Article.objects.create(title="Old Title", content="Old Content", author="Author 1")

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        update_serializer_class = ArticleCreateSchema

    with TestClient(api) as client:
        response = client.put(
            f"/articles/{article.id}",
            json={"data": {"title": "Updated Title", "content": "Updated Content", "author": "Author 1"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated Title"
        article.refresh_from_db()
        assert article.title == "Updated Title"


@pytest.mark.parametrize(
    "view_classes",
    [
        (PartialUpdateMixin, ViewSet),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_partial_update_mixin(api, view_classes: list[type]):
    article = Article.objects.create(title="Old Title", content="Old Content", author="Author 1")

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    with TestClient(api) as client:
        response = client.patch(
            f"/articles/{article.id}",
            json={"data": {"title": "Patched Title"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Patched Title"
        assert data["content"] == "Old Content"
        article.refresh_from_db()
        assert article.title == "Patched Title"


@pytest.mark.parametrize(
    "view_classes",
    [
        (DestroyMixin, ViewSet),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_destroy_mixin(api, view_classes: list[type]):
    article = Article.objects.create(title="To Delete", content="Content", author="Author 1")

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    with TestClient(api) as client:
        response = client.delete(f"/articles/{article.id}")
        assert response.status_code == 204
        assert not Article.objects.filter(id=article.id).exists()


@pytest.mark.parametrize(
    "view_classes",
    [
        (RetrieveMixin, ViewSet),
        (ReadOnlyModelViewSet,),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_retrieve_by_custom_lookup_field(api, view_classes: list[type]):
    # Create test data
    article = Article.objects.create(title="title1", content="Content 1", author="Author 1")

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        lookup_field = "title"

    with TestClient(api) as client:
        # Retrieve by title instead of id
        response = client.get("/articles/title1")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == article.id
        assert data["title"] == "title1"

        # Try to retrieve by non-existent title
        response = client.get("/articles/nonexistent")
        assert response.status_code == 404


@pytest.mark.parametrize(
    "view_classes",
    [
        (CreateMixin, UpdateMixin, PartialUpdateMixin, DestroyMixin, ViewSet),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_write_by_custom_lookup_field(api, view_classes: list[type]):
    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        create_serializer_class = ArticleCreateSchema
        update_serializer_class = ArticleCreateSchema
        lookup_field = "title"

    with TestClient(api) as client:
        # Test create
        response = client.post(
            "/articles",
            json={"data": {"title": "unique_title", "content": "Content", "author": "Author"}},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "unique_title"
        assert Article.objects.filter(title="unique_title").exists()

        # Test update by title
        response = client.put(
            "/articles/unique_title",
            json={"data": {"title": "unique_title", "content": "Updated Content", "author": "Updated Author"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["content"] == "Updated Content"
        assert data["author"] == "Updated Author"
        article = Article.objects.get(title="unique_title")
        assert article.content == "Updated Content"

        # Test partial update by title
        response = client.patch(
            "/articles/unique_title",
            json={"data": {"content": "Patched Content"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["content"] == "Patched Content"
        assert data["author"] == "Updated Author"
        article.refresh_from_db()
        assert article.content == "Patched Content"

        # Test delete by title
        response = client.delete("/articles/unique_title")
        assert response.status_code == 204
        assert not Article.objects.filter(title="unique_title").exists()


@pytest.mark.parametrize(
    "view_classes",
    [
        (ListMixin, RetrieveMixin, ViewSet),
        (ReadOnlyModelViewSet,),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_list_queryset_reevaluation(api, view_classes: list[type]):
    """Test that queryset is re-evaluated on each request (like DRF)."""

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    with TestClient(api) as client:
        # First request - empty
        response = client.get("/articles")
        assert response.status_code == 200
        assert len(response.json()) == 0

        # Create article outside the viewset
        article = Article.objects.create(
            title="Article 1",
            content="Content 1",
            author="Author 1",
        )

        # Second request - should see the new article (queryset re-evaluated)
        response = client.get("/articles")
        assert response.status_code == 200
        response_data = response.json()
        assert len(response_data) == 1
        assert response_data[0]["id"] == article.id
        assert response_data[0]["title"] == "Article 1"

        response = client.get(f"/articles/{article.id}")
        assert response.status_code == 200
        assert response.json() == response_data[0]


@pytest.mark.parametrize(
    "view_classes",
    [
        (ListMixin, ViewSet),
        (ReadOnlyModelViewSet,),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_list_custom_queryset(api, view_classes: list[type]):
    """Test ModelViewSet with custom get_queryset()."""
    # Create test data
    Article.objects.create(
        title="Published 1",
        content="Content",
        author="Author",
        is_published=True,
    )
    Article.objects.create(
        title="Draft 1",
        content="Content",
        author="Author",
        is_published=False,
    )

    @api.viewset("/articles/published")
    class PublishedArticleViewSet(*view_classes):
        queryset = Article.objects.all()  # Base queryset
        serializer_class = ArticleSchema

        async def get_queryset(self):
            # Custom filtering
            queryset = await super().get_queryset()
            return queryset.filter(is_published=True)

    with TestClient(api) as client:
        response = client.get("/articles/published")
        assert response.status_code == 200
        data = response.json()
        # Should only get published articles
        assert len(data) == 1
        assert data[0]["is_published"] is True
        assert data[0]["title"] == "Published 1"


@pytest.mark.parametrize("view_set_class", [ViewSet, ReadOnlyModelViewSet, ModelViewSet])
@pytest.mark.django_db(transaction=True)
def test_list_with_pagination(api, sample_articles, view_set_class: type):
    """Test ViewSet with pagination."""

    @api.viewset("/articles")
    class ArticleListViewSet(view_set_class):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        pagination_class = PageNumberPagination

        if view_set_class == ViewSet:

            async def list(self, request):
                return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles?page=2&page_size=10")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 2
        assert len(data["items"]) == 10
        assert data["total"] == 45

        response = client.get("/articles?page=5&page_size=10")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 5
        assert len(data["items"]) == 5
        assert data["total"] == 45


@pytest.mark.parametrize("view_set_class", [ViewSet, ReadOnlyModelViewSet, ModelViewSet])
@pytest.mark.django_db(transaction=True)
def test_list_with_paginate_decorator(api, sample_articles, view_set_class: type):
    """Test ViewSet with pagination."""

    @api.viewset("/articles")
    class ArticleListViewSet(view_set_class):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        if view_set_class == ViewSet:

            @paginate(PageNumberPagination)
            async def list(self, request):
                return Article.objects.all()
        else:

            @paginate(PageNumberPagination)
            async def list(self, request):
                return await super().list(request)

    with TestClient(api) as client:
        response = client.get("/articles?page=2&page_size=10")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 2
        assert len(data["items"]) == 10
        assert data["total"] == 45

        response = client.get("/articles?page=5&page_size=10")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 5
        assert len(data["items"]) == 5
        assert data["total"] == 45


@pytest.mark.parametrize(
    "view_classes",
    [
        (ListMixin, RetrieveMixin, CreateMixin, UpdateMixin, PartialUpdateMixin, ViewSet),
        (ModelViewSet,),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_list_and_retrieve_different_serializers(api, view_classes: list[type]):
    # Create test data
    article1 = Article.objects.create(title="Title 1", content="Content 1", author="Author 1")
    article2 = Article.objects.create(title="Title 2", content="Content 2", author="Author 2")

    class ArticleUpdateSchema(Serializer):
        title: str
        content: str

    @api.viewset("/articles")
    class ArticleViewSet(*view_classes):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        list_serializer_class = ArticleListSchema
        create_serializer_class = ArticleCreateSchema
        update_serializer_class = ArticleUpdateSchema

    with TestClient(api) as client:
        # Test list uses ArticleListSchema (only id and title)
        response = client.get("/articles")
        assert response.status_code == 200
        list_data = response.json()
        assert len(list_data) == 2

        # ArticleListSchema should only have id and title
        assert "id" in list_data[0]
        assert "title" in list_data[0]
        assert "content" not in list_data[0]
        assert "author" not in list_data[0]
        assert "is_published" not in list_data[0]

        # Test retrieve uses ArticleSchema (all fields)
        response = client.get(f"/articles/{article1.id}")
        assert response.status_code == 200
        retrieve_data = response.json()

        # ArticleSchema should have all fields
        assert retrieve_data["id"] == article1.id
        assert retrieve_data["title"] == "Title 1"
        assert retrieve_data["content"] == "Content 1"
        assert retrieve_data["author"] == "Author 1"
        assert "is_published" in retrieve_data

        # Test create uses ArticleCreateSchema (accepts title, content, author)
        response = client.post(
            "/articles",
            json={"data": {"title": "New Title", "content": "New Content", "author": "New Author"}},
        )
        assert response.status_code == 201
        create_data = response.json()

        # Response should use default serializer_class (ArticleSchema) with all fields
        assert "id" in create_data
        assert create_data["title"] == "New Title"
        assert create_data["content"] == "New Content"
        assert create_data["author"] == "New Author"
        assert "is_published" in create_data

        new_article_id = create_data["id"]
        assert Article.objects.filter(id=new_article_id).exists()

        # Test update uses ArticleUpdateSchema
        # accept title, content
        # ignore author
        response = client.put(
            f"/articles/{article1.id}",
            json={"data": {"title": "Updated Title", "content": "Updated Content", "author": "Updated Author"}},
        )
        assert response.status_code == 200
        update_data = response.json()

        # Response should use default serializer_class (ArticleSchema) with all fields
        assert update_data["id"] == article1.id
        assert update_data["title"] == "Updated Title"
        assert update_data["content"] == "Updated Content"
        assert update_data["author"] == "Author 1"  # Unchanged
        assert "is_published" in update_data

        article1.refresh_from_db()
        assert article1.title == "Updated Title"
        assert article1.content == "Updated Content"
        assert article1.author == "Author 1"

        # Test patch uses ArticleUpdateSchema
        # accept title, content
        # ignore author
        response = client.patch(
            f"/articles/{article2.id}",
            json={"data": {"title": "Patched Title", "author": "Patched Author"}},
        )
        assert response.status_code == 200
        patch_data = response.json()

        # Response should use default serializer_class (ArticleSchema) with all fields
        assert patch_data["id"] == article2.id
        assert patch_data["title"] == "Patched Title"
        assert patch_data["content"] == "Content 2"  # Unchanged
        assert patch_data["author"] == "Author 2"  # Unchanged
        assert "is_published" in patch_data

        article2.refresh_from_db()
        assert article2.title == "Patched Title"
        assert article2.content == "Content 2"
        assert article2.author == "Author 2"
