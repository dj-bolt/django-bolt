"""A QuerySet response with a nested Struct for a relation (#913).

A ``list[Struct]`` response type loads a QuerySet with ``.values()``. A field
whose type is a Struct names a foreign key or a one-to-one relation. Bolt
reads the fields of the nested Struct across the relation, in the same query.
"""

from __future__ import annotations

from typing import Annotated

import msgspec
import pytest
from django.db.backends import utils as db_utils
from django.db.models.functions import JSONObject

from django_bolt import BoltAPI, ViewSet
from django_bolt.pagination import CursorPagination, PageNumberPagination, paginate
from django_bolt.responses import JSON
from django_bolt.testing import TestClient
from django_bolt.views import RetrieveMixin

from .test_models import Author, BlogPost, Comment, User, UserProfile


class AuthorOut(msgspec.Struct):
    id: int
    name: str


class PostOut(msgspec.Struct):
    id: int
    title: str
    author: AuthorOut


class AnnotatedPost(msgspec.Struct):
    title: str
    author: Annotated[AuthorOut, msgspec.Meta(description="The author")]


class AnnotatedOptionalPost(msgspec.Struct):
    title: str
    author: Annotated[AuthorOut | None, msgspec.Meta(description="The author")] = None


class OptionalAnnotatedPost(msgspec.Struct):
    title: str
    author: Annotated[AuthorOut, msgspec.Meta(description="The author")] | None = None


class RenamedAuthor(msgspec.Struct):
    name: str = msgspec.field(name="authorName")


class RenamedPost(msgspec.Struct):
    title: str = msgspec.field(name="postTitle")
    author: RenamedAuthor = msgspec.field(name="postAuthor")


class PostTitle(msgspec.Struct):
    title: str
    author: AuthorOut


class CommentOut(msgspec.Struct):
    text: str
    post: PostTitle


class UserOut(msgspec.Struct):
    username: str


class ProfileOut(msgspec.Struct):
    bio: str


class ProfileWithUser(msgspec.Struct):
    bio: str
    user: UserOut


class UserWithProfile(msgspec.Struct):
    username: str
    profile: ProfileOut | None


class TitleMeta(msgspec.Struct):
    id: int
    title: str


class PostWithMeta(msgspec.Struct):
    meta: TitleMeta
    author: AuthorOut


class PostOfLaterAuthor(msgspec.Struct):
    title: str
    author: LaterAuthor  # noqa: F821 - the test defines it after the route


class ProfileOfUser(msgspec.Struct):
    bio: str
    user: UserOfProfile


class UserOfProfile(msgspec.Struct):
    username: str
    profile: ProfileOfUser | None


def _make_posts() -> None:
    ada = Author.objects.create(name="Ada", email="ada@example.com")
    bob = Author.objects.create(name="Bob", email="bob@example.com")
    BlogPost.objects.create(title="First", content="x", author=ada)
    BlogPost.objects.create(title="Second", content="y", author=bob)


@pytest.fixture
def sql_statements(monkeypatch):
    """Collect the SQL statements of every thread. The ORM executor has its own connections."""
    statements: list[str] = []
    original = db_utils.CursorWrapper._execute

    def collecting_execute(self, sql, params, *wrapper_args):
        statements.append(sql)
        return original(self, sql, params, *wrapper_args)

    monkeypatch.setattr(db_utils.CursorWrapper, "_execute", collecting_execute)
    return statements


@pytest.mark.django_db(transaction=True)
def test_nested_struct_of_a_foreign_key(sql_statements):
    _make_posts()
    api = BoltAPI()

    @api.get("/async")
    async def posts_async() -> list[PostOut]:
        return BlogPost.objects.select_related("author").order_by("id")

    @api.get("/sync")
    def posts_sync() -> list[PostOut]:
        return BlogPost.objects.order_by("id")

    @api.get("/unvalidated", validate_response=False)
    async def posts_unvalidated() -> list[PostOut]:
        return BlogPost.objects.order_by("id")

    expected = [
        {"id": post.id, "title": post.title, "author": {"id": post.author_id, "name": post.author.name}}
        for post in BlogPost.objects.select_related("author").order_by("id")
    ]
    with TestClient(api) as client:
        for path in ("/async", "/sync", "/unvalidated"):
            sql_statements.clear()
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == expected
            # One query with a join, not one query for each author.
            assert len(sql_statements) == 1, sql_statements


@pytest.mark.django_db(transaction=True)
def test_nested_struct_uses_the_wire_names():
    _make_posts()
    api = BoltAPI()

    @api.get("/posts")
    async def posts() -> list[RenamedPost]:
        return BlogPost.objects.order_by("id")

    with TestClient(api) as client:
        response = client.get("/posts")
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"postTitle": "First", "postAuthor": {"authorName": "Ada"}},
        {"postTitle": "Second", "postAuthor": {"authorName": "Bob"}},
    ]


@pytest.mark.django_db(transaction=True)
def test_nested_struct_with_annotated_metadata():
    """``Annotated[Nested, msgspec.Meta(...)]`` is a nested Struct of a relation too."""
    _make_posts()
    api = BoltAPI()

    @api.get("/annotated")
    async def annotated() -> list[AnnotatedPost]:
        return BlogPost.objects.order_by("id")

    @api.get("/annotated-optional")
    async def annotated_optional() -> list[AnnotatedOptionalPost]:
        return BlogPost.objects.order_by("id")

    @api.get("/optional-annotated")
    async def optional_annotated() -> list[OptionalAnnotatedPost]:
        return BlogPost.objects.order_by("id")

    with TestClient(api) as client:
        for path in ("/annotated", "/annotated-optional", "/optional-annotated"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert [row["author"]["name"] for row in response.json()] == ["Ada", "Bob"], path


@pytest.mark.django_db(transaction=True)
def test_nested_struct_two_levels_deep():
    _make_posts()
    post = BlogPost.objects.get(title="First")
    Comment.objects.create(post=post, author=post.author, text="Nice")
    api = BoltAPI()

    @api.get("/comments")
    async def comments() -> list[CommentOut]:
        return Comment.objects.all()

    with TestClient(api) as client:
        response = client.get("/comments")
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"text": "Nice", "post": {"title": "First", "author": {"id": post.author_id, "name": "Ada"}}}
    ]


@pytest.mark.django_db(transaction=True)
def test_nested_struct_of_a_one_to_one_relation():
    with_profile = User.objects.create(username="ada", email="ada@example.com", password_hash="x")
    User.objects.create(username="bob", email="bob@example.com", password_hash="x")
    UserProfile.objects.create(user=with_profile, bio="Hi")
    api = BoltAPI()

    @api.get("/profiles")
    async def profiles() -> list[ProfileWithUser]:
        return UserProfile.objects.all()

    @api.get("/users")
    async def users() -> list[UserWithProfile]:
        return User.objects.order_by("username")

    with TestClient(api) as client:
        profile_response = client.get("/profiles")
        assert profile_response.status_code == 200, profile_response.text
        assert profile_response.json() == [{"bio": "Hi", "user": {"username": "ada"}}]

        # A reverse one-to-one with no row gives None.
        user_response = client.get("/users")
        assert user_response.status_code == 200, user_response.text
        assert user_response.json() == [
            {"username": "ada", "profile": {"bio": "Hi"}},
            {"username": "bob", "profile": None},
        ]


@pytest.mark.django_db(transaction=True)
def test_nested_struct_that_is_not_a_relation_is_read_whole():
    _make_posts()
    api = BoltAPI()

    @api.get("/posts")
    async def posts() -> list[PostWithMeta]:
        return BlogPost.objects.annotate(meta=JSONObject(id="id", title="title")).order_by("id")

    with TestClient(api) as client:
        response = client.get("/posts")
    assert response.status_code == 200, response.text
    assert [(row["meta"]["title"], row["author"]["name"]) for row in response.json()] == [
        ("First", "Ada"),
        ("Second", "Bob"),
    ]


@pytest.mark.django_db(transaction=True)
def test_nested_struct_defined_after_the_route(monkeypatch):
    """A forward reference can resolve after the route registers, as with no nested Struct."""
    _make_posts()
    api = BoltAPI()

    @api.get("/posts")
    async def posts() -> list[PostOfLaterAuthor]:
        return BlogPost.objects.order_by("id")

    monkeypatch.setitem(globals(), "LaterAuthor", RenamedAuthor)
    with TestClient(api) as client:
        response = client.get("/posts")
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"title": "First", "author": {"authorName": "Ada"}},
        {"title": "Second", "author": {"authorName": "Bob"}},
    ]


@pytest.mark.django_db(transaction=True)
def test_struct_that_nests_itself_across_relations_gives_a_clear_error():
    user = User.objects.create(username="ada", email="ada@example.com", password_hash="x")
    UserProfile.objects.create(user=user, bio="Hi")
    api = BoltAPI()

    @api.get("/profiles")
    async def profiles() -> list[ProfileOfUser]:
        return UserProfile.objects.all()

    message = "ProfileOfUser nests itself across the relation UserOfProfile.profile"
    with TestClient(api) as client, pytest.raises(TypeError, match=message):
        client.get("/profiles")


# A model instance with a nested Struct of a relation (#929). Bolt loads the
# relations that the Struct names before it reads the instance.


class UserWithDefaultProfile(msgspec.Struct):
    username: str
    profile: ProfileOut | None = None


class TagOut(msgspec.Struct):
    name: str


class PostWithTags(msgspec.Struct):
    title: str
    tags: list[TagOut]


class PostOnlyTitle(msgspec.Struct):
    title: str


class AuthorWithPosts(msgspec.Struct):
    name: str
    posts: list[PostOnlyTitle]


def _expected_posts() -> list[dict]:
    return [
        {"id": post.id, "title": post.title, "author": {"id": post.author_id, "name": post.author.name}}
        for post in BlogPost.objects.select_related("author").order_by("id")
    ]


@pytest.mark.django_db(transaction=True)
def test_paginated_nested_struct_loads_the_relation(sql_statements):
    _make_posts()
    api = BoltAPI()

    @api.get("/async")
    @paginate(PageNumberPagination)
    async def page_async() -> list[PostOut]:
        return BlogPost.objects.order_by("id")

    @api.get("/sync")
    @paginate(PageNumberPagination)
    def page_sync() -> list[PostOut]:
        return BlogPost.objects.order_by("id")

    @api.get("/joined")
    @paginate(PageNumberPagination)
    async def page_joined() -> list[PostOut]:
        return BlogPost.objects.select_related("author").order_by("id")

    @api.get("/cursor")
    @paginate(CursorPagination)
    async def page_cursor() -> list[PostOut]:
        return BlogPost.objects.all()

    @api.get("/list")
    @paginate(PageNumberPagination)
    async def page_list() -> list[PostOut]:
        return [post async for post in BlogPost.objects.order_by("id")]

    expected = _expected_posts()
    # The count and one page query with a join. The cursor page has no count.
    # The list has the query of the handler and one query for the authors.
    cases = (("/async", expected, 2), ("/sync", expected, 2), ("/joined", expected, 2))
    cases += (("/cursor", expected[::-1], 1), ("/list", expected, 2))
    with TestClient(api) as client:
        for path, items, queries in cases:
            sql_statements.clear()
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json()["items"] == items, path
            assert len(sql_statements) == queries, (path, sql_statements)


@pytest.mark.django_db(transaction=True)
def test_paginated_nested_struct_two_levels_deep(sql_statements):
    _make_posts()
    for post in BlogPost.objects.order_by("id"):
        Comment.objects.create(post=post, author=post.author, text=f"On {post.title}")
    api = BoltAPI()

    @api.get("/comments")
    @paginate(PageNumberPagination)
    async def comments() -> list[CommentOut]:
        return Comment.objects.order_by("id")

    with TestClient(api) as client:
        sql_statements.clear()
        response = client.get("/comments")
    assert response.status_code == 200, response.text
    assert [(row["post"]["title"], row["post"]["author"]["name"]) for row in response.json()["items"]] == [
        ("First", "Ada"),
        ("Second", "Bob"),
    ]
    assert len(sql_statements) == 2, sql_statements


@pytest.mark.django_db(transaction=True)
def test_paginated_nested_struct_with_a_default_reads_the_relation(sql_statements):
    """A relation that could not load gave the default of the field, not the related row."""
    with_profile = User.objects.create(username="ada", email="ada@example.com", password_hash="x")
    User.objects.create(username="bob", email="bob@example.com", password_hash="x")
    UserProfile.objects.create(user=with_profile, bio="Hi")
    api = BoltAPI()

    @api.get("/users")
    @paginate(PageNumberPagination)
    async def users() -> list[UserWithDefaultProfile]:
        return User.objects.order_by("username")

    with TestClient(api) as client:
        sql_statements.clear()
        response = client.get("/users")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [
        {"username": "ada", "profile": {"bio": "Hi"}},
        {"username": "bob", "profile": None},
    ]
    assert len(sql_statements) == 2, sql_statements


@pytest.mark.django_db(transaction=True)
def test_viewset_retrieve_with_a_nested_struct(sql_statements):
    _make_posts()
    post = BlogPost.objects.get(title="First")
    Comment.objects.create(post=post, author=post.author, text="Nice")
    comment_id = Comment.objects.get().id
    api = BoltAPI()

    @api.viewset("/posts")
    class PostViewSet(RetrieveMixin, ViewSet):
        queryset = BlogPost.objects.all()
        serializer_class = PostOut

    @api.viewset("/comments")
    class CommentViewSet(RetrieveMixin, ViewSet):
        queryset = Comment.objects.all()
        serializer_class = CommentOut

    @api.get("/handler/{pk}")
    async def get_post(pk: int) -> PostOut:
        return await BlogPost.objects.aget(pk=pk)

    @api.get("/sync-handler/{pk}")
    def get_post_sync(pk: int) -> PostOut:
        return BlogPost.objects.get(pk=pk)

    with TestClient(api) as client:
        # The object, then one query for each relation that is not loaded.
        for path, queries in ((f"/posts/{post.id}", 2), (f"/handler/{post.id}", 2), (f"/sync-handler/{post.id}", 2)):
            sql_statements.clear()
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == {"id": post.id, "title": "First", "author": {"id": post.author_id, "name": "Ada"}}
            assert len(sql_statements) == queries, (path, sql_statements)

        sql_statements.clear()
        response = client.get(f"/comments/{comment_id}")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "text": "Nice",
            "post": {"title": "First", "author": {"id": post.author_id, "name": "Ada"}},
        }
        assert len(sql_statements) == 3, sql_statements


@pytest.mark.django_db(transaction=True)
def test_list_of_instances_with_a_nested_struct(sql_statements):
    _make_posts()
    api = BoltAPI()

    @api.get("/unvalidated-joined", validate_response=False)
    async def unvalidated_joined() -> list[PostOut]:
        return [post async for post in BlogPost.objects.select_related("author").order_by("id")]

    @api.get("/unvalidated", validate_response=False)
    async def unvalidated() -> list[PostOut]:
        return [post async for post in BlogPost.objects.order_by("id")]

    @api.get("/unvalidated-sync", validate_response=False)
    def unvalidated_sync() -> list[PostOut]:
        return list(BlogPost.objects.order_by("id"))

    @api.get("/validated")
    async def validated() -> list[PostOut]:
        return [post async for post in BlogPost.objects.order_by("id")]

    @api.get("/validated-sync")
    def validated_sync() -> list[PostOut]:
        return list(BlogPost.objects.order_by("id"))

    expected = _expected_posts()
    # The query of the handler, and one query for the authors that it did not load.
    cases = (("/unvalidated-joined", 1), ("/unvalidated", 2), ("/unvalidated-sync", 2))
    cases += (("/validated", 2), ("/validated-sync", 2))
    with TestClient(api) as client:
        for path, queries in cases:
            sql_statements.clear()
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == expected, path
            assert len(sql_statements) == queries, (path, sql_statements)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("item_type", "field"), [(PostWithTags, "PostWithTags.tags"), (AuthorWithPosts, "AuthorWithPosts.posts")]
)
def test_nested_struct_of_a_many_relation_gives_a_clear_error(item_type, field):
    """A reverse foreign key or a many-to-many relation has many rows. ``.values()`` cannot nest them."""
    _make_posts()
    model = BlogPost if item_type is PostWithTags else Author
    api = BoltAPI()

    @api.get("/items", response_model=list[item_type])
    async def items():
        return model.objects.all()

    @api.get("/page", response_model=list[item_type])
    @paginate(PageNumberPagination)
    async def page():
        return model.objects.all()

    with TestClient(api) as client:
        for path in ("/items", "/page"):
            with pytest.raises(TypeError, match=field):
                client.get(path)


class PostTitleOnly(msgspec.Struct):
    id: int
    title: str


async def _load_posts(select_author: bool) -> list[BlogPost]:
    queryset = BlogPost.objects.select_related("author") if select_author else BlogPost.objects.all()
    return [post async for post in queryset.order_by("id")]


@pytest.mark.django_db(transaction=True)
def test_list_of_instances_from_a_helper_without_validation(sql_statements):
    """A helper hides the ORM calls, so the route takes the executor that encodes a list as it is."""
    _make_posts()
    api = BoltAPI()

    @api.get("/nested", validate_response=False)
    async def nested(joined: bool = False) -> list[PostOut]:
        return await _load_posts(joined)

    @api.get("/flat", validate_response=False)
    async def flat(joined: bool = False) -> list[PostTitleOnly]:
        return await _load_posts(joined)

    expected = _expected_posts()
    with TestClient(api) as client:
        for path, queries in (("/nested", 2), ("/nested?joined=true", 1)):
            sql_statements.clear()
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == expected, path
            assert len(sql_statements) == queries, (path, sql_statements)

        response = client.get("/flat")
        assert response.status_code == 200, response.text
        assert response.json() == [{"id": post["id"], "title": post["title"]} for post in expected]


type PostAlias = PostOut
type PostListAlias = list[PostOut]


@pytest.mark.django_db(transaction=True)
def test_model_instance_with_a_written_out_response_type(sql_statements):
    """``Annotated``, a ``type`` alias and ``-> Item | JSON`` validate against the Struct, so they load its relations too."""
    _make_posts()
    post = BlogPost.objects.order_by("id").first()
    api = BoltAPI()

    @api.get("/annotated/{pk}")
    async def annotated(pk: int) -> Annotated[PostOut, msgspec.Meta(description="A post")]:
        return await BlogPost.objects.aget(pk=pk)

    @api.get("/union/{pk}")
    async def union(pk: int) -> PostOut | JSON:
        return await BlogPost.objects.aget(pk=pk)

    @api.get("/alias/{pk}")
    async def alias(pk: int) -> PostAlias:
        return await BlogPost.objects.aget(pk=pk)

    @api.get("/list-alias")
    async def list_alias() -> PostListAlias:
        return [item async for item in BlogPost.objects.order_by("id")]

    expected = {"id": post.id, "title": post.title, "author": {"id": post.author_id, "name": "Ada"}}
    expected_list = _expected_posts()
    with TestClient(api) as client:
        for path in (f"/annotated/{post.id}", f"/union/{post.id}", f"/alias/{post.id}"):
            sql_statements.clear()
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == expected, path
            assert len(sql_statements) == 2, (path, sql_statements)

        sql_statements.clear()
        response = client.get("/list-alias")
        assert response.status_code == 200, response.text
        assert response.json() == expected_list
        # The query of the handler, and one query for the authors.
        assert len(sql_statements) == 2, sql_statements
