import builtins

import pytest

from django_bolt import BoltAPI, ModelViewSet, PageNumberPagination, ReadOnlyModelViewSet, ViewSet, action, paginate
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient
from tests.test_models import Article


@pytest.fixture
def api():
    """Create a fresh BoltAPI instance for each test."""
    return BoltAPI()


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


# --- Schemas ---


class ArticleSchema(Serializer):
    id: int
    title: str
    content: str
    author: str
    is_published: bool


class ArticleCreateSchema(Serializer):
    title: str
    content: str
    author: str


class ArticlePatchSchema(Serializer):
    title: str | None
    content: str | None
    author: str | None


# --- Tests ---


@pytest.mark.parametrize("view_set_class", [ViewSet, ReadOnlyModelViewSet, ModelViewSet])
@pytest.mark.django_db(transaction=True)
def test_viewset_with_custom_methods(api, view_set_class: type):
    """Test ViewSet with custom methods."""

    @api.viewset("/articles")
    class ArticleListViewSet(view_set_class):
        queryset = Article.objects.all()

        async def list(self, request) -> builtins.list[ArticleSchema]:
            return Article.objects.all()

        async def retrieve(self, request) -> ArticleCreateSchema:
            return await self.get_object()

        async def create(self, request, data: ArticleCreateSchema) -> ArticleSchema:
            article = await Article.objects.acreate(
                title=data.title + " custom create",
                content=data.content,
                author=data.author,
            )
            return article

        async def update(self, request, data: ArticleCreateSchema) -> ArticleCreateSchema:
            article = await Article.objects.aget(**{self.lookup_field: self.request.params[self.lookup_field]})
            article.title = data.title
            article.content = data.content
            article.author = data.author
            await article.asave()
            return article

        async def partial_update(self, request, data: ArticlePatchSchema) -> ArticleCreateSchema:
            article = await Article.objects.aget(**{self.lookup_field: self.request.params[self.lookup_field]})
            if data.title is not None:
                article.title = data.title
            if data.content is not None:
                article.content = data.content
            if data.author is not None:
                article.author = data.author
            await article.asave()
            return article

        async def destroy(self, request) -> None:
            article = await Article.objects.aget(**{self.lookup_field: self.request.params[self.lookup_field]})
            await article.adelete()
            return None

    with TestClient(api) as client:
        # List
        response = client.get("/articles")
        assert response.status_code == 200
        assert response.json() == []

        # Create
        response = client.post(
            "/articles",
            json={"title": "New Article", "content": "New Content", "author": "Test Author"},
        )
        assert response.status_code == 201
        response_data = response.json()
        assert response_data["title"] == "New Article custom create"
        article_id = response_data["id"]

        # Retrieve
        response = client.get(f"/articles/{article_id}")
        assert response.status_code == 200
        assert response.json() == {
            "title": "New Article custom create",
            "content": "New Content",
            "author": "Test Author",
        }

        # Update
        response = client.put(
            f"/articles/{article_id}",
            json={"title": "Updated Title", "content": "Updated Content", "author": "Updated Author"},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "Updated Title"

        # Partial update
        response = client.patch(
            f"/articles/{article_id}",
            json={"title": "Patched Title", "content": None, "author": None},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "Patched Title"

        # Delete
        response = client.delete(f"/articles/{article_id}")
        assert response.status_code == 204

        # Verify deletion
        response = client.get(f"/articles/{article_id}")
        assert response.status_code == 404


@pytest.mark.parametrize("view_set_class", [ViewSet, ReadOnlyModelViewSet, ModelViewSet])
@pytest.mark.django_db(transaction=True)
def test_viewset_custom_action_with_pagination(api, sample_articles, view_set_class: type):
    """Test ViewSet custom action with pagination."""

    @api.viewset("/articles")
    class ArticleListViewSet(view_set_class):
        serializer_class = ArticleSchema

        @action(["GET"], False, response_model=list[ArticleSchema])
        @paginate(PageNumberPagination)
        async def custom_list(self, request):
            return Article.objects.all()

    with TestClient(api) as client:
        response = client.get("/articles/custom_list?page=2&page_size=10")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 2
        assert len(data["items"]) == 10
        assert data["total"] == 45

        response = client.get("/articles/custom_list?page=5&page_size=10")
        assert response.status_code == 200

        data = response.json()
        assert data["page"] == 5
        assert len(data["items"]) == 5
        assert data["total"] == 45


@pytest.mark.parametrize("view_set_class", [ViewSet, ReadOnlyModelViewSet, ModelViewSet])
@pytest.mark.django_db(transaction=True)
def test_viewset_custom_action_with_return_annotation(api, sample_articles, view_set_class: type):
    """Test ViewSet custom action with return type annotation."""

    @api.viewset("/articles")
    class ArticleListViewSet(view_set_class):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        @action(["GET"], detail=False)
        async def recent(self, request) -> list[ArticleSchema]:
            return Article.objects.all().order_by("-id")[:5]

        @action(["GET"], detail=True)
        async def summary(self, request) -> ArticleCreateSchema:
            return await self.get_object()

    with TestClient(api) as client:
        # Test collection action with list return type
        response = client.get("/articles/recent")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 5
        # Verify schema filtering (ArticleSchema includes id, title, content, author, is_published)
        assert "id" in data[0]
        assert "is_published" in data[0]

        # Test detail action with single object return type
        article = sample_articles[0]
        response = client.get(f"/articles/{article.pk}/summary")
        assert response.status_code == 200
        data = response.json()
        # ArticleCreateSchema only has title, content, author (no id, no is_published)
        assert "title" in data
        assert "id" not in data
        assert "is_published" not in data
        assert data["title"] == article.title


@pytest.mark.parametrize("view_set_class", [ViewSet, ReadOnlyModelViewSet, ModelViewSet])
@pytest.mark.django_db(transaction=True)
def test_viewset_action_inheritance(api, view_set_class: type):
    """Test that actions are correctly inherited in ViewSets."""

    @api.viewset("/base")
    class BaseViewSet(view_set_class):
        serializer_class = ArticleSchema

        @action(["GET"], detail=False)
        async def shared_action(self, request):
            return {"message": "from base"}

        @action(["GET"], detail=True)
        async def shared_detail_action(self, request):
            return {"message": f"detail from base {self.request.params['pk']}"}

    @api.viewset("/inherited")
    class ChildViewSet(BaseViewSet):
        @action(["GET"], detail=False)
        async def child_action(self, request):
            return {"message": "from child"}

    with TestClient(api) as client:
        # Test base action
        response = client.get("/base/shared_action")
        assert response.status_code == 200
        assert response.json() == {"message": "from base"}

        response = client.get("/base/456/shared_detail_action")
        assert response.status_code == 200
        assert response.json() == {"message": "detail from base 456"}

        # Test inherited action
        response = client.get("/inherited/shared_action")
        assert response.status_code == 200
        assert response.json() == {"message": "from base"}

        response = client.get("/inherited/123/shared_detail_action")
        assert response.status_code == 200
        assert response.json() == {"message": "detail from base 123"}

        # Test child's own action
        response = client.get("/inherited/child_action")
        assert response.status_code == 200
        assert response.json() == {"message": "from child"}
