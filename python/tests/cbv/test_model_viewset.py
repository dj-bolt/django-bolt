"""
Tests for ModelViewSet and ReadOnlyModelViewSet (DRF-style usage).

This test suite verifies that ModelViewSet and ReadOnlyModelViewSet work similarly
to Django REST Framework's ModelViewSet, where you just set queryset and serializer_class.
"""

import pytest

from django_bolt import BoltAPI, ModelViewSet, ReadOnlyModelViewSet, action
from django_bolt.serializers import Serializer, field
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
            json={"title": "New Article", "content": "New Content", "author": "Test Author"},
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
            json={"title": "Updated Title", "content": "Updated Content", "author": "Updated Author"},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "Updated Title"

        # Partial update
        response = client.patch(
            f"/articles/{article_id}",
            json={"title": "Patched Title"},
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
@pytest.mark.parametrize("lookup_value", ["abc", "1.5"])
def test_lookup_value_of_the_wrong_type_is_not_found(api, lookup_value):
    """A lookup value that the field cannot hold answers 404, as an unknown value does."""

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    article = Article.objects.create(title="t", content="c", author="a")
    body = {"id": article.id, "title": "new", "content": "c", "author": "a", "is_published": False}

    with TestClient(api) as client:
        responses = [
            client.get(f"/articles/{lookup_value}"),
            client.put(f"/articles/{lookup_value}", json=body),
            client.patch(f"/articles/{lookup_value}", json={"title": "new"}),
            client.delete(f"/articles/{lookup_value}"),
        ]
        for response in responses:
            assert response.status_code == 404
            assert response.json() == {"detail": "Not found"}

        assert client.get(f"/articles/{article.id}").json()["title"] == "t"


@pytest.mark.django_db(transaction=True)
def test_viewset_decorator_lookup_field_reaches_get_object(api):
    """`@api.viewset(..., lookup_field=)` selects the lookup of the detail actions."""

    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        @action(methods=["GET"], detail=True)
        async def summary(self, request):
            article = await self.get_object()
            return {"id": article.id, "lookup_field": self.lookup_field}

    # One class, two registrations with different lookup fields.
    api.viewset("/by-title", lookup_field="title")(ArticleViewSet)
    api.viewset("/by-pk")(ArticleViewSet)

    first = Article.objects.create(title="t1", content="c", author="a")
    second = Article.objects.create(title="t2", content="c", author="a")

    with TestClient(api) as client:
        response = client.get("/by-title/t2")
        assert response.status_code == 200
        assert response.json()["id"] == second.id

        response = client.patch("/by-title/t2", json={"content": "new"})
        assert response.status_code == 200
        assert response.json()["content"] == "new"

        response = client.get("/by-title/t1/summary")
        assert response.status_code == 200
        assert response.json() == {"id": first.id, "lookup_field": "title"}

        assert client.delete("/by-title/t1").status_code == 204
        assert client.get("/by-title/t1").status_code == 404

        # The other registration of the same class still uses the primary key.
        response = client.get(f"/by-pk/{second.id}")
        assert response.status_code == 200
        assert response.json()["title"] == "t2"
        response = client.get(f"/by-pk/{second.id}/summary")
        assert response.status_code == 200
        assert response.json() == {"id": second.id, "lookup_field": "pk"}

    assert ArticleViewSet.lookup_field == "pk"


@pytest.mark.django_db(transaction=True)
def test_write_actions_do_not_write_outside_the_queryset(api):
    """The request body cannot select the row that PUT, PATCH or POST writes."""

    class ReadOnlyIdSchema(Serializer):
        title: str
        content: str
        author: str
        id: int = field(read_only=True, default=0)

    @api.viewset("/mine")
    class MineViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

        async def get_queryset(self):
            return (await super().get_queryset()).filter(author="me")

    @api.viewset("/read-only-id")
    class ReadOnlyIdViewSet(MineViewSet):
        serializer_class = ReadOnlyIdSchema

    @api.viewset("/by-title")
    class ByTitleViewSet(MineViewSet):
        lookup_field = "title"

    mine = Article.objects.create(title="mine", content="c", author="me")
    other = Article.objects.create(title="other", content="c", author="other")

    def rows():
        return list(Article.objects.order_by("id").values_list("id", "title", "author", "is_published"))

    unchanged = [(mine.id, "mine", "me", False), (other.id, "other", "other", False)]
    full_body = {"title": "put", "content": "c", "author": "me", "is_published": True}

    with TestClient(api) as client:
        # The row of the other author is not in get_queryset().
        assert client.patch(f"/mine/{other.id}", json={"title": "x"}).status_code == 404

        # PATCH: the primary key of another row, and a primary key that no row has.
        for body_id in (other.id, 777):
            response = client.patch(f"/mine/{mine.id}", json={"id": body_id})
            assert response.status_code == 200
            assert response.json()["id"] == mine.id
            assert rows() == unchanged

        # PATCH: the other fields of the body apply to the row of the URL.
        response = client.patch(f"/mine/{mine.id}", json={"id": other.id, "title": "patched"})
        assert response.status_code == 200
        assert response.json()["id"] == mine.id
        assert rows() == [(mine.id, "patched", "me", False), unchanged[1]]

        # PUT: the same, with a plain `id` field and with a read-only `id` field.
        for prefix in ("/mine", "/read-only-id"):
            response = client.put(f"{prefix}/{mine.id}", json={"id": other.id, **full_body})
            assert response.status_code == 200
            assert response.json()["id"] == mine.id
            assert rows()[1] == unchanged[1]
            assert len(rows()) == 2
        assert rows()[0] == (mine.id, "put", "me", True)

        # PUT and PATCH do not change the lookup field of the object.
        response = client.put("/by-title/put", json={"id": other.id, **full_body, "title": "other"})
        assert response.status_code == 200
        assert response.json()["title"] == "put"
        response = client.patch("/by-title/put", json={"title": "other", "content": "new"})
        assert response.status_code == 200
        assert response.json() == {
            "id": mine.id,
            "title": "put",
            "content": "new",
            "author": "me",
            "is_published": True,
        }
        assert rows() == [(mine.id, "put", "me", True), unchanged[1]]

        # POST: an `id` in the body does not select an existing row.
        for prefix in ("/mine", "/read-only-id"):
            response = client.post(prefix, json={"id": other.id, **full_body, "title": "created"})
            assert response.status_code == 201
            assert response.json()["id"] not in (mine.id, other.id)
            assert rows()[1] == unchanged[1]
        assert [row[1] for row in rows()] == ["put", "other", "created", "created"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("body", [b"{bad", b"", b"[1, 2]", b'"x"'])
def test_write_actions_answer_422_for_a_body_that_is_not_a_json_object(api, body):
    """POST, PUT and PATCH give the 422 of a function route for the same bad body."""

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    @api.post("/function")
    async def function_route(data: ArticleSchema):
        return {"ok": True}

    article = Article.objects.create(title="t", content="c", author="a")
    request = {"content": body, "headers": {"content-type": "application/json"}}

    with TestClient(api) as client:
        expected = client.post("/function", **request)
        assert expected.status_code == 422
        assert expected.json()["detail"][0]["loc"][0] == "body"

        responses = [
            client.post("/articles", **request),
            client.put(f"/articles/{article.id}", **request),
            client.patch(f"/articles/{article.id}", **request),
        ]
        for response in responses:
            assert response.status_code == 422
            assert response.json() == expected.json()

    assert list(Article.objects.values_list("id", "title")) == [(article.id, "t")]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"title": "new"},
        {"title": 1, "content": 2, "author": "a"},
        {"title": "new", "content": "c", "author": None},
    ],
    ids=["no-field", "two-missing-fields", "two-wrong-types", "null"],
)
def test_write_actions_answer_the_422_of_a_function_route_for_a_bad_field(api, body):
    """One body gives one 422 body: the write actions do not have their own error shape."""

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema

    @api.post("/function")
    async def function_route(data: ArticleSchema):
        return {"ok": True}

    article = Article.objects.create(title="t", content="c", author="a")

    with TestClient(api) as client:
        expected = client.post("/function", json=body)
        assert expected.status_code == 422
        assert len(expected.json()["detail"]) == 1

        assert client.post("/articles", json=body).json() == expected.json()
        assert client.put(f"/articles/{article.id}", json=body).json() == expected.json()
        # PATCH takes a subset of the fields, so it reports only a bad value.
        if not {"title", "content", "author"} - set(body):
            assert client.patch(f"/articles/{article.id}", json=body).json() == expected.json()

    assert list(Article.objects.values_list("id", "title")) == [(article.id, "t")]


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
