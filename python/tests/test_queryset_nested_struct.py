"""A QuerySet response with a nested Struct for a relation (#913).

A ``list[Struct]`` response type loads a QuerySet with ``.values()``. A field
whose type is a Struct names a foreign key or a one-to-one relation. Bolt
reads the fields of the nested Struct across the relation, in the same query.
"""

from __future__ import annotations

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

from .test_models import Author, BlogPost, Comment, User, UserProfile


class AuthorOut(msgspec.Struct):
    id: int
    name: str


class PostOut(msgspec.Struct):
    id: int
    title: str
    author: AuthorOut


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


def _make_posts() -> None:
    ada = Author.objects.create(name="Ada", email="ada@example.com")
    bob = Author.objects.create(name="Bob", email="bob@example.com")
    BlogPost.objects.create(title="First", content="x", author=ada)
    BlogPost.objects.create(title="Second", content="y", author=bob)


@pytest.mark.django_db(transaction=True)
def test_nested_struct_of_a_foreign_key():
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
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == expected


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
