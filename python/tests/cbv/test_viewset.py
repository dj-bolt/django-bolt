import builtins

import msgspec
import pytest

from django_bolt import BoltAPI, ModelViewSet, PageNumberPagination, ReadOnlyModelViewSet, ViewSet, action, paginate
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient
from django_bolt.views import APIView
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


class _Pair(msgspec.Struct):
    id: int


class _TwoPerPage(PageNumberPagination):
    page_size = 2


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_custom_action_gets_the_view_instance_and_its_arguments(api, kind):
    """A sync or async @action method runs on a view instance with the request, the action name and the path value."""

    def describe(view, pk: int) -> dict:
        return {"view": type(view).__name__, "action": view.action, "path": view.request["path"], "pk": pk}

    @api.viewset("/things")
    class ThingViewSet(ViewSet):
        @action(methods=["GET"], detail=True)
        def sync_info(self, request, pk: int) -> dict:
            """Describe one thing."""
            return describe(self, pk)

        @action(methods=["GET"], detail=True)
        async def async_info(self, request, pk: int) -> dict:
            """Describe one thing."""
            return describe(self, pk)

    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get(f"/things/7/{kind}_info")
        operation = client.get("/docs/openapi.json").json()["paths"][f"/things/{{pk}}/{kind}_info"]["get"]

    assert response.status_code == 200
    assert response.json() == {
        "view": "ThingViewSet",
        "action": f"{kind}_info",
        "path": f"/things/7/{kind}_info",
        "pk": 7,
    }
    # The docstring of the method documents the route.
    assert operation["summary"] == "Describe one thing."


def test_viewset_class_settings_apply_to_its_routes(api):
    """``status_code`` of the class applies to the standard actions, and ``validate_response`` also to an @action."""

    @api.viewset("/things")
    class ThingViewSet(ViewSet):
        status_code = 202
        validate_response = False

        async def list(self, request) -> list[_Pair]:
            return [{"id": "not a number"}]

        async def create(self, request) -> dict:
            return {"created": True}

        @action(methods=["GET"], detail=False)
        async def unchecked(self, request) -> _Pair:
            return {"id": "not a number"}

        @action(methods=["GET"], detail=False, validate_response=True)
        async def checked(self, request) -> _Pair:
            return {"id": "not a number"}

    with TestClient(api) as client:
        # With validation on, these bodies give 500, because they are not a _Pair.
        response = client.get("/things")
        assert (response.status_code, response.json()) == (202, [{"id": "not a number"}])
        assert client.get("/things/unchecked").json() == {"id": "not a number"}

        # The status code of the class replaces the 201 of the create action.
        assert client.post("/things").status_code == 202
        # The argument of @action wins over the class.
        assert client.get("/things/checked").status_code == 500


@pytest.mark.parametrize("kind", ["view", "viewset"])
def test_validate_response_attribute_of_a_method(api, kind):
    """A ``validate_response`` attribute on the method function sets the validation of its route."""

    async def unchecked(self, request) -> _Pair:
        return {"id": "not a number"}

    unchecked.validate_response = False

    if kind == "view":

        @api.view("/things")
        class ThingView(APIView):
            get = unchecked

    else:

        @api.viewset("/things")
        class ThingViewSet(ViewSet):
            list = unchecked

    with TestClient(api) as client:
        response = client.get("/things")

    # With validation on, this body gives 500, because it is not a _Pair.
    assert response.status_code == 200
    assert response.json() == {"id": "not a number"}


def test_viewset_list_with_its_own_paginate_is_paginated_one_time(api):
    """A list action with @paginate keeps that pagination when the class also sets ``pagination_class``."""

    @api.viewset("/things")
    class ThingViewSet(ViewSet):
        pagination_class = PageNumberPagination

        @paginate(_TwoPerPage)
        async def list(self, request):
            return [{"id": number} for number in range(5)]

    with TestClient(api) as client:
        response = client.get("/things")

    assert response.status_code == 200
    page = response.json()
    assert page["items"] == [{"id": 0}, {"id": 1}]
    assert (page["total"], page["page_size"]) == (5, 2)


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
            return {"message": f"detail from base {request.params['pk']}"}

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


@pytest.mark.django_db(transaction=True)
def test_viewset_get_object_accepts_explicit_lookup_values(api):
    article = Article.objects.create(title="Lookup Title", content="Lookup Content", author="Author 1")

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        lookup_field = "id"

        async def retrieve(self, request, id: int) -> ArticleSchema:
            if request.query.get("mode") == "keyword":
                return await self.get_object(id=id)
            return await self.get_object(id)

    with TestClient(api) as client:
        response = client.get(f"/articles/{article.id}")
        assert response.status_code == 200
        assert response.json()["title"] == "Lookup Title"

        response = client.get(f"/articles/{article.id}?mode=keyword")
        assert response.status_code == 200
        assert response.json()["title"] == "Lookup Title"


@pytest.mark.django_db(transaction=True)
def test_viewset_registration_supports_explicit_action_serializer_selection(api):
    article = Article.objects.create(title="List Title", content="Detail Content", author="Author 1")

    class ArticleListSchema(Serializer):
        id: int
        title: str

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()

        def get_serializer_class(self, action: str | None = None):
            return ArticleListSchema if action == "list" else ArticleSchema

        async def list(self, request):
            return [{"id": article.id, "title": article.title}]

        async def retrieve(self, request):
            return {
                "id": article.id,
                "title": article.title,
                "content": article.content,
                "author": article.author,
                "is_published": article.is_published,
            }

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200
        assert response.json() == [{"id": article.id, "title": article.title}]

        response = client.get(f"/articles/{article.id}")
        assert response.status_code == 200
        assert response.json()["content"] == "Detail Content"


def test_viewset_context_access_outside_request_raises():
    view = ViewSet()

    with pytest.raises(LookupError):
        _ = view.request

    with pytest.raises(LookupError):
        _ = view.action


@pytest.mark.django_db(transaction=True)
def test_viewset_inherits_parent_queryset(api):
    Article.objects.create(title="Inherited Title", content="Inherited Content", author="Author 1")

    class BaseArticleViewSet(ReadOnlyModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    @api.viewset("/articles")
    class ArticleViewSet(BaseArticleViewSet):
        pass

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200
        assert response.json()[0]["title"] == "Inherited Title"
