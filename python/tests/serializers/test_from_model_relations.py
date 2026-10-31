from __future__ import annotations

import asyncio
from typing import Any

import pytest
from msgspec import structs as msgspec_structs

from django_bolt import BoltAPI
from django_bolt.exceptions import SerializationError
from django_bolt.serializers import Serializer, field
from django_bolt.testing import TestClient
from tests.test_models import Author, BlogPost, Comment, User, UserProfile


class BlogPostMiniSerializer(Serializer):
    id: int
    title: str


class CommentMiniSerializer(Serializer):
    id: int
    text: str


class UserMiniSerializer(Serializer):
    id: int
    username: str


class AuthorMiniSerializer(Serializer):
    id: int
    name: str


class AuthorWithPlainPostsSerializer(Serializer):
    id: int
    name: str
    posts: list[BlogPostMiniSerializer] = field(default_factory=list)


class AuthorWithRequiredPostsSerializer(Serializer):
    id: int
    name: str
    posts: list[BlogPostMiniSerializer]


class BlogPostWithCommentsSerializer(Serializer):
    id: int
    title: str
    comments: list[CommentMiniSerializer] = field(default_factory=list)


class UserProfileOutputSerializer(Serializer):
    id: int
    bio: str
    user: UserMiniSerializer


class UserProfileMiniSerializer(Serializer):
    id: int
    bio: str


class UserWithProfileBioSerializer(Serializer):
    id: int
    username: str
    profile_bio: str | None = field(source="profile.bio", default=None)


class UserWithProfileSerializer(Serializer):
    id: int
    username: str
    profile: UserProfileMiniSerializer
    profile_bio: str | None = field(source="profile.bio", default=None)


class BlogPostWithAuthorAndCommentsSerializer(Serializer):
    id: int
    title: str
    author: AuthorMiniSerializer
    comments: list[CommentMiniSerializer] = field(default_factory=list)


class AuthorPostIdsSerializer(Serializer):
    id: int
    post_ids: list[int] = field(default_factory=list)


class AuthorPayloadSerializer(Serializer):
    id: int
    payload: Any = None


class UserProfileAsDictSerializer(Serializer):
    """A relation field with no nested serializer and no default.

    Its type does not show that it is a relation.
    """

    id: int
    profile: dict | None


class UserProfileIdSerializer(Serializer):
    """A relation field that reads the primary key of the related object."""

    id: int
    profile: int | None


@pytest.mark.django_db
def test_from_model_plain_reverse_relation_prefetched_returns_nested_list():
    author = Author.objects.create(name="Alice", email="alice@example.com")
    BlogPost.objects.create(title="Post 1", content="One", author=author)
    BlogPost.objects.create(title="Post 2", content="Two", author=author)

    author = Author.objects.prefetch_related("posts").get(id=author.id)
    serializer = AuthorWithPlainPostsSerializer.from_model(author)

    assert len(serializer.posts) == 2
    assert all(isinstance(post, BlogPostMiniSerializer) for post in serializer.posts)
    assert {post.title for post in serializer.posts} == {"Post 1", "Post 2"}


@pytest.mark.django_db
def test_from_model_plain_reverse_relation_unprefetched_uses_default():
    author = Author.objects.create(name="Alice", email="alice@example.com")
    BlogPost.objects.create(title="Post 1", content="One", author=author)

    author = Author.objects.get(id=author.id)
    serializer = AuthorWithPlainPostsSerializer.from_model(author)

    assert serializer.posts == []


@pytest.mark.django_db
def test_from_model_required_unloaded_reverse_relation_raises_serialization_error():
    author = Author.objects.create(name="Alice", email="alice@example.com")
    BlogPost.objects.create(title="Post 1", content="One", author=author)

    author = Author.objects.get(id=author.id)

    with pytest.raises(SerializationError) as exc_info:
        AuthorWithRequiredPostsSerializer.from_model(author)

    error_message = str(exc_info.value)
    assert "posts" in error_message
    assert "prefetch_related('posts')" in error_message
    assert "afrom_model" in error_message


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_afrom_model_plain_reverse_relation_lazy_loads():
    author = await Author.objects.acreate(name="Alice", email="alice@example.com")
    await BlogPost.objects.acreate(title="Post 1", content="One", author=author)
    await BlogPost.objects.acreate(title="Post 2", content="Two", author=author)

    author = await Author.objects.aget(id=author.id)
    serializer = await AuthorWithPlainPostsSerializer.afrom_model(author)

    assert len(serializer.posts) == 2
    assert all(isinstance(post, BlogPostMiniSerializer) for post in serializer.posts)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_afrom_model_nested_many_lazy_loads_for_reverse_fk():
    author = await Author.objects.acreate(name="Alice", email="alice@example.com")
    post = await BlogPost.objects.acreate(title="Post 1", content="One", author=author)
    commenter = await Author.objects.acreate(name="Bob", email="bob@example.com")
    await Comment.objects.acreate(post=post, author=commenter, text="Nice post")

    post = await BlogPost.objects.aget(id=post.id)
    comments_serializer = await BlogPostWithCommentsSerializer.afrom_model(post)

    assert len(comments_serializer.comments) == 1
    assert isinstance(comments_serializer.comments[0], CommentMiniSerializer)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_afrom_model_lazy_loads_forward_fk_and_source_paths():
    user = await User.objects.acreate(
        username="alice",
        email="alice@example.com",
        password_hash="hash",
    )
    profile = await UserProfile.objects.acreate(user=user, bio="Hello", location="Earth")

    profile = await UserProfile.objects.aget(id=profile.id)
    profile_serializer = await UserProfileOutputSerializer.afrom_model(profile)
    user = await User.objects.aget(id=user.id)
    user_serializer = await UserWithProfileBioSerializer.afrom_model(user)

    assert isinstance(profile_serializer.user, UserMiniSerializer)
    assert profile_serializer.user.username == "alice"
    assert user_serializer.profile_bio == "Hello"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_afrom_model_reuses_inflight_relation_load_for_shared_source_path(monkeypatch):
    user = await User.objects.acreate(
        username="alice",
        email="alice@example.com",
        password_hash="hash",
    )
    await UserProfile.objects.acreate(user=user, bio="Hello", location="Earth")

    user = await User.objects.aget(id=user.id)
    original_loader = Serializer._load_relation_async.__func__
    load_calls: list[str] = []

    async def tracked_loader(cls, obj, relation):
        if obj.__class__ is User and obj.pk == user.pk:
            load_calls.append(relation.name)
        return await original_loader(cls, obj, relation)

    monkeypatch.setattr(
        UserWithProfileSerializer,
        "_load_relation_async",
        classmethod(tracked_loader),
    )

    serializer = await UserWithProfileSerializer.afrom_model(user)

    assert serializer.profile.bio == "Hello"
    assert serializer.profile_bio == "Hello"
    assert load_calls == ["profile"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_afrom_model_loads_multiple_unloaded_relations_concurrently(monkeypatch):
    author = await Author.objects.acreate(name="Alice", email="alice@example.com")
    post = await BlogPost.objects.acreate(title="Post 1", content="One", author=author)
    commenter = await Author.objects.acreate(name="Bob", email="bob@example.com")
    await Comment.objects.acreate(post=post, author=commenter, text="Nice post")

    post = await BlogPost.objects.aget(id=post.id)
    original_loader = Serializer._load_relation_async.__func__
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    seen_relations: list[str] = []

    async def tracked_loader(cls, obj, relation):
        if obj.__class__ is BlogPost and obj.pk == post.pk and relation.name in {"author", "comments"}:
            seen_relations.append(relation.name)
            if not first_started.is_set():
                first_started.set()
                await asyncio.wait_for(second_started.wait(), timeout=0.5)
            else:
                second_started.set()
        return await original_loader(cls, obj, relation)

    monkeypatch.setattr(
        BlogPostWithAuthorAndCommentsSerializer,
        "_load_relation_async",
        classmethod(tracked_loader),
    )

    serializer = await BlogPostWithAuthorAndCommentsSerializer.afrom_model(post)

    assert serializer.author.name == "Alice"
    assert len(serializer.comments) == 1
    assert set(seen_relations) == {"author", "comments"}


@pytest.mark.django_db
def test_from_model_unloaded_source_path_uses_default_without_querying():
    user = User.objects.create(
        username="alice",
        email="alice@example.com",
        password_hash="hash",
    )
    UserProfile.objects.create(user=user, bio="Hello", location="Earth")

    user = User.objects.get(id=user.id)
    serializer = UserWithProfileBioSerializer.from_model(user)

    assert serializer.profile_bio is None


@pytest.mark.django_db
def test_dump_fails_fast_when_manager_leaks_into_serializer_state():
    author = Author.objects.create(name="Alice", email="alice@example.com")
    serializer = AuthorWithPlainPostsSerializer(id=1, name="Alice")

    # Simulate a broken serializer state caused by bypassing from_model().
    msgspec_structs.force_setattr(serializer, "posts", author.posts)

    with pytest.raises(SerializationError) as exc_info:
        serializer.dump()

    assert "Serialize Django relations with from_model()/afrom_model()" in str(exc_info.value)


@pytest.mark.django_db
def test_scalar_list_fields_are_not_marked_for_orm_state_checks():
    class AuthorTagNamesSerializer(Serializer):
        id: int
        tag_names: list[str] = field(default_factory=list)

    assert AuthorPostIdsSerializer.__orm_state_check_fields__ == ()
    assert AuthorTagNamesSerializer.__orm_state_check_fields__ == ()


@pytest.mark.django_db
def test_dump_fast_path_still_guards_any_fields_for_manager_leaks():
    author = Author.objects.create(name="Alice", email="alice@example.com")
    serializer = AuthorPayloadSerializer(id=author.id)

    msgspec_structs.force_setattr(serializer, "payload", author.posts)

    with pytest.raises(SerializationError) as exc_info:
        serializer.dump()

    assert "payload" in str(exc_info.value)


@pytest.mark.django_db
def test_dump_many_fast_path_still_guards_any_fields_for_manager_leaks():
    author = Author.objects.create(name="Alice", email="alice@example.com")
    serializer = AuthorPayloadSerializer(id=author.id)

    msgspec_structs.force_setattr(serializer, "payload", author.posts)

    with pytest.raises(SerializationError) as exc_info:
        AuthorPayloadSerializer.dump_many([serializer])

    assert "payload" in str(exc_info.value)


@pytest.mark.django_db(transaction=True)
def test_async_route_loads_a_relation_that_its_field_type_does_not_show():
    """An async route loads an unloaded relation with afrom_model().

    It does this also when the type of the field does not show a relation.
    """
    user = User.objects.create(username="solo", email="solo@example.com", password_hash="x")
    api = BoltAPI()

    @api.get("/user")
    async def get_user() -> UserProfileAsDictSerializer:
        return await User.objects.aget(pk=user.pk)

    @api.get("/users")
    async def list_users() -> list[UserProfileAsDictSerializer]:
        return [await User.objects.aget(pk=user.pk)]

    with TestClient(api) as client:
        resp = client.get("/user")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"id": user.pk, "profile": None}

        resp = client.get("/users")
        assert resp.status_code == 200, resp.text
        assert resp.json() == [{"id": user.pk, "profile": None}]


@pytest.mark.django_db(transaction=True)
def test_async_route_loads_an_existing_relation_that_its_field_type_does_not_show():
    """An async route loads an existing profile that the query did not load."""
    user = User.objects.create(username="owner", email="owner@example.com", password_hash="x")
    profile = UserProfile.objects.create(user=user, bio="hello")
    api = BoltAPI()

    @api.get("/user")
    async def get_user() -> UserProfileIdSerializer:
        return await User.objects.aget(pk=user.pk)

    @api.get("/users")
    async def list_users() -> list[UserProfileIdSerializer]:
        return [await User.objects.aget(pk=user.pk)]

    with TestClient(api) as client:
        resp = client.get("/user")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"id": user.pk, "profile": profile.pk}

        resp = client.get("/users")
        assert resp.status_code == 200, resp.text
        assert resp.json() == [{"id": user.pk, "profile": profile.pk}]
