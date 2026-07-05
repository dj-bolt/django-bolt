"""Regression tests for ModelViewSet serializer output."""

from __future__ import annotations

import msgspec
import pytest

from django_bolt import BoltAPI, ModelViewSet
from django_bolt.serializers import Serializer, computed_field, field
from django_bolt.testing import TestClient
from tests.test_models import Article, Author, BlogPost


class BaseArticle(Serializer):
    id: int
    title: str


class ArticleOut(BaseArticle):
    content: str

    @computed_field
    def title_upper(self) -> str:
        return self.title.upper()


class SecretArticle(Serializer):
    id: int
    title: str
    content: str = field(write_only=True)


class PlainArticle(msgspec.Struct):
    id: int
    title: str
    content: str


class AuthorMini(Serializer):
    id: int
    name: str


class PostOut(Serializer):
    id: int
    title: str
    author: AuthorMini

    @computed_field
    def title_len(self) -> int:
        return len(self.title)


class AuthorPostIds(Serializer):
    id: int
    posts: list[int]


@pytest.fixture
def article():
    return Article.objects.create(title="hello", content="the body", author="A1")


@pytest.mark.django_db(transaction=True)
def test_default_list_and_retrieve_use_serializer_dump(article):
    api = BoltAPI()

    @api.viewset("/articles")
    class VS(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleOut

    expected = {
        "id": article.id,
        "title": "hello",
        "content": "the body",
        "title_upper": "HELLO",
    }

    with TestClient(api) as client:
        r = client.get("/articles")
        assert r.status_code == 200, r.text
        assert r.json() == [expected]

        r = client.get(f"/articles/{article.id}")
        assert r.status_code == 200, r.text
        assert r.json() == expected


@pytest.mark.django_db(transaction=True)
def test_plain_struct_viewset_keeps_values_projection(article):
    api = BoltAPI()

    @api.viewset("/articles")
    class VS(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = PlainArticle

    list_meta = next(
        api._handler_meta[hid]
        for method, path, hid, _h in api._routes
        if method == "GET" and path == "/articles"
    )
    assert list_meta["response_field_names"] == ["id", "title", "content"]

    with TestClient(api) as client:
        r = client.get("/articles")
        assert r.status_code == 200, r.text
        assert r.json() == [{"id": article.id, "title": "hello", "content": "the body"}]


@pytest.mark.django_db(transaction=True)
def test_nested_serializer_list_and_retrieve():
    api = BoltAPI()
    author = Author.objects.create(name="Ada", email="ada@x.io")
    post = BlogPost.objects.create(title="Post", content="body", author=author)

    @api.viewset("/posts")
    class VS(ModelViewSet):
        queryset = BlogPost.objects.select_related("author").all()
        serializer_class = PostOut

    expected = {
        "id": post.id,
        "title": "Post",
        "author": {"id": author.id, "name": "Ada"},
        "title_len": 4,
    }

    with TestClient(api) as client:
        r = client.get("/posts")
        assert r.status_code == 200, r.text
        assert r.json() == [expected]

        r = client.get(f"/posts/{post.id}")
        assert r.status_code == 200, r.text
        assert r.json() == expected


@pytest.mark.django_db(transaction=True)
def test_loaded_nested_serializer_uses_sync_from_model(monkeypatch):
    api = BoltAPI()
    author = Author.objects.create(name="Ada", email="ada@x.io")
    post = BlogPost.objects.create(title="Post", content="body", author=author)

    async def fail_afrom_model(cls, instance, *, _depth=0, max_depth=10):
        raise AssertionError("select_related nested output should not need afrom_model()")

    monkeypatch.setattr(PostOut, "afrom_model", classmethod(fail_afrom_model))

    @api.viewset("/posts")
    class VS(ModelViewSet):
        queryset = BlogPost.objects.select_related("author").all()
        serializer_class = PostOut

    with TestClient(api) as client:
        r = client.get("/posts")
        assert r.status_code == 200, r.text
        assert r.json() == [
            {
                "id": post.id,
                "title": "Post",
                "author": {"id": author.id, "name": "Ada"},
                "title_len": 4,
            }
        ]


@pytest.mark.django_db(transaction=True)
def test_unloaded_nested_serializer_falls_back_to_afrom_model():
    api = BoltAPI()
    author = Author.objects.create(name="Ada", email="ada@x.io")
    post = BlogPost.objects.create(title="Post", content="body", author=author)

    @api.viewset("/posts")
    class VS(ModelViewSet):
        queryset = BlogPost.objects.all()
        serializer_class = PostOut

    with TestClient(api) as client:
        r = client.get("/posts")
        assert r.status_code == 200, r.text
        assert r.json() == [
            {
                "id": post.id,
                "title": "Post",
                "author": {"id": author.id, "name": "Ada"},
                "title_len": 4,
            }
        ]


@pytest.mark.django_db(transaction=True)
def test_direct_reverse_relation_field_uses_async_fallback():
    api = BoltAPI()
    author = Author.objects.create(name="Ada", email="ada@x.io")
    post = BlogPost.objects.create(title="Post", content="body", author=author)

    @api.get("/authors", response_model=list[AuthorPostIds])
    async def authors():
        return Author.objects.all()

    with TestClient(api) as client:
        r = client.get("/authors")
        assert r.status_code == 200, r.text
        assert r.json() == [{"id": author.id, "posts": [post.id]}]


@pytest.mark.django_db(transaction=True)
def test_serializer_instances_with_write_only_fields_are_dumped(article):
    api = BoltAPI()

    @api.get("/one/{pk}", response_model=SecretArticle)
    async def one(pk: int):
        return SecretArticle.from_model(await Article.objects.aget(id=pk))

    @api.get("/many", response_model=list[SecretArticle])
    async def many():
        return [SecretArticle.from_model(a) async for a in Article.objects.all()]

    with TestClient(api) as client:
        r = client.get(f"/one/{article.id}")
        assert r.status_code == 200, r.text
        assert r.json() == {"id": article.id, "title": "hello"}

        r = client.get("/many")
        assert r.status_code == 200, r.text
        assert r.json() == [{"id": article.id, "title": "hello"}]


@pytest.mark.django_db(transaction=True)
def test_serializer_response_still_renders_with_validation_disabled(article):
    api = BoltAPI()

    @api.get("/one/{pk}", response_model=ArticleOut, validate_response=False)
    async def one(pk: int):
        return await Article.objects.aget(id=pk)

    with TestClient(api) as client:
        r = client.get(f"/one/{article.id}")
        assert r.status_code == 200, r.text
        assert r.json() == {
            "id": article.id,
            "title": "hello",
            "content": "the body",
            "title_upper": "HELLO",
        }
