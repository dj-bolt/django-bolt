"""
Serializer loading plan (#292 / #295): a Serializer derives the ORM loads it
needs from its own fields, and Bolt applies them wherever it evaluates a
QuerySet for that serializer.

- ``Serializer.from_models(qs)`` / ``Serializer.loading_plan(model)``:
  FK / O2O → ``select_related`` (nested serializers recurse),
  M2M / reverse FK → ``prefetch_related`` (nested plan inside ``Prefetch``),
  dotted ``field(source="a.b")`` → ``select_related("a")``,
  ``Config.annotations`` → ``annotate``.
- Additive: loads the caller already asked for are kept, never duplicated.
- Automatic: a ``list[Serializer]`` route (plain or ``@paginate``) returning a
  bare QuerySet is served with the plan applied — no N+1, no user code.
"""

from __future__ import annotations

import pytest
from django.db.backends import utils as db_utils
from django.db.models import Count, F, Prefetch

from django_bolt import BoltAPI
from django_bolt.pagination import PageNumberPagination, paginate
from django_bolt.serializers import Serializer, field
from django_bolt.testing import TestClient
from tests.test_models import Author, BlogPost, Comment, Tag

try:  # Django >= 6.1
    from django.db.models import fetch_modes
except ImportError:  # pragma: no cover
    fetch_modes = None

needs_fetch_modes = pytest.mark.skipif(fetch_modes is None, reason="fetch modes need Django >= 6.1")


class AuthorMini(Serializer):
    id: int
    name: str


class TagMini(Serializer):
    id: int
    name: str


class CommentWithAuthor(Serializer):
    id: int
    text: str
    author: AuthorMini


class PostWithAuthor(Serializer):
    id: int
    title: str
    author: AuthorMini


class PostFull(Serializer):
    id: int
    title: str
    author: AuthorMini
    tags: list[TagMini]
    comments: list[CommentWithAuthor]


class PostWithAuthorEmail(Serializer):
    id: int
    author_email: str = field(source="author.email")


class AuthorWithPostCount(Serializer):
    class Config:
        annotations = {"post_count": Count("posts")}

    id: int
    name: str
    post_count: int


@pytest.fixture
def blog(db):
    author = Author.objects.create(name="Alice", email="alice@example.com")
    tag = Tag.objects.create(name="python")
    posts = []
    for i in range(3):
        post = BlogPost.objects.create(title=f"Post {i}", content="c", author=author)
        post.tags.add(tag)
        Comment.objects.create(post=post, author=author, text=f"comment {i}")
        posts.append(post)
    return author, posts


@pytest.fixture
def count_queries(monkeypatch, blog):
    """Count SQL statements on every thread (the ORM executor uses its own connections).

    Depends on ``blog`` so the fixture data is inserted before counting starts,
    whatever order a test lists its parameters in.
    """
    statements: list[str] = []
    original = db_utils.CursorWrapper._execute

    def counting_execute(self, sql, params, *ignored_wrapper_args):
        statements.append(sql)
        return original(self, sql, params, *ignored_wrapper_args)

    monkeypatch.setattr(db_utils.CursorWrapper, "_execute", counting_execute)
    return statements


# ---------------------------------------------------------------------------
# Plan derivation
# ---------------------------------------------------------------------------


def test_plan_select_related_for_single_valued_relation():
    plan = PostWithAuthor.loading_plan(BlogPost)
    assert plan.select_related == ("author",)
    assert plan.prefetch == ()


def test_plan_prefetch_for_many_valued_relations_with_nested_plan():
    plan = PostFull.loading_plan(BlogPost)
    assert plan.select_related == ("author",)
    lookups = {(p.prefetch_to if isinstance(p, Prefetch) else p) for p in plan.prefetch}
    assert lookups == {"tags", "comments"}
    # comments → CommentWithAuthor needs comment.author: nested plan rides inside the Prefetch
    comments = next(p for p in plan.prefetch if isinstance(p, Prefetch) and p.prefetch_to == "comments")
    assert comments.queryset.query.select_related == {"author": {}}
    # tags → TagMini has no relations: plain string lookup, no nested queryset
    assert "tags" in plan.prefetch


def test_plan_follows_dotted_source():
    assert PostWithAuthorEmail.loading_plan(BlogPost).select_related == ("author",)


def test_plan_carries_config_annotations():
    assert list(AuthorWithPostCount.loading_plan(Author).annotations) == ["post_count"]


def test_plan_skips_join_for_fk_id_only_field():
    class PostAuthorId(Serializer):
        id: int
        author: int  # reads author_id; the related row is never touched

    assert not PostAuthorId.loading_plan(BlogPost)


def test_plan_ignores_columns_and_unknown_attributes():
    class Plain(Serializer):
        id: int
        title: str
        comment_total: int = field(source="not_a_field")

    plan = Plain.loading_plan(BlogPost)
    assert not plan


# ---------------------------------------------------------------------------
# from_models(): explicit use
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_from_models_loads_nested_relations_in_bounded_queries(blog, django_assert_num_queries):
    # posts+author, tags, comments(+author via nested select_related)
    with django_assert_num_queries(3):
        dumped = PostFull.dump_many(PostFull.from_models(BlogPost.objects.order_by("id")))

    assert dumped[0]["author"] == {"id": blog[0].id, "name": "Alice"}
    assert [t["name"] for t in dumped[0]["tags"]] == ["python"]
    assert dumped[2]["comments"][0]["author"]["name"] == "Alice"


@pytest.mark.django_db
def test_from_models_applies_annotations(blog, django_assert_num_queries):
    with django_assert_num_queries(1):
        dumped = AuthorWithPostCount.dump_many(AuthorWithPostCount.from_models(Author.objects.all()))
    assert dumped == [{"id": blog[0].id, "name": "Alice", "post_count": 3}]


@pytest.mark.django_db
def test_from_models_keeps_callers_loads_and_never_duplicates(blog, django_assert_num_queries):
    base = BlogPost.objects.prefetch_related("tags").select_related("author").order_by("id")

    with django_assert_num_queries(3):  # posts+author, tags (once), comments
        posts = PostFull.from_models(base)
    assert [p.title for p in posts] == ["Post 0", "Post 1", "Post 2"]


@pytest.mark.django_db
def test_from_models_leaves_values_querysets_alone(blog):
    """A ``.values()`` queryset yields dicts, not models: no select_related on it (TypeError)."""
    values_qs = BlogPost.objects.order_by("id").values("id", author_email=F("author__email"))
    rows = PostWithAuthorEmail._prepare_queryset(values_qs)
    assert list(rows) == [{"id": p.id, "author_email": "alice@example.com"} for p in blog[1]]


@pytest.mark.django_db(transaction=True)
def test_paginated_values_queryset_still_works(blog, count_queries):
    api = BoltAPI()

    @api.get("/posts")
    @paginate(PageNumberPagination)
    async def list_posts(request) -> list[PostWithAuthorEmail]:
        return BlogPost.objects.order_by("id").values("id", author_email=F("author__email"))

    with TestClient(api) as client:
        response = client.get("/posts")

    assert response.status_code == 200
    assert response.json()["items"][0]["author_email"] == "alice@example.com"


@pytest.mark.django_db
def test_from_models_accepts_plain_iterables(blog):
    posts = PostWithAuthor.from_models(list(BlogPost.objects.select_related("author").order_by("id")))
    assert [p.author.name for p in posts] == ["Alice"] * 3


@pytest.mark.django_db(transaction=True)
def test_afrom_models_in_async_route(blog, count_queries):
    api = BoltAPI()

    @api.get("/posts")
    async def list_posts() -> list[PostFull]:
        return await PostFull.afrom_models(BlogPost.objects.order_by("id"))

    with TestClient(api) as client:
        response = client.get("/posts")

    assert response.status_code == 200
    assert response.json()[0]["comments"][0]["author"]["name"] == "Alice"
    assert len(count_queries) == 3


# ---------------------------------------------------------------------------
# Automatic: routes and pagination
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_list_route_returning_bare_queryset_is_served_without_n_plus_one(blog, count_queries):
    api = BoltAPI()

    @api.get("/posts")
    async def list_posts() -> list[PostFull]:
        return BlogPost.objects.order_by("id")

    with TestClient(api) as client:
        response = client.get("/posts")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3
    assert body[0]["author"]["name"] == "Alice"
    assert [t["name"] for t in body[0]["tags"]] == ["python"]
    assert body[1]["comments"][0]["author"]["name"] == "Alice"
    assert len(count_queries) == 3  # posts+author, tags, comments+author


@pytest.mark.django_db(transaction=True)
def test_sync_list_route_returning_bare_queryset_is_served_without_n_plus_one(blog, count_queries):
    api = BoltAPI()

    @api.get("/posts")
    def list_posts() -> list[PostFull]:
        return BlogPost.objects.order_by("id")

    with TestClient(api) as client:
        response = client.get("/posts")

    assert response.status_code == 200
    assert response.json()[2]["comments"][0]["text"] == "comment 2"
    assert len(count_queries) == 3


@pytest.mark.django_db(transaction=True)
def test_plain_serializer_route_gets_config_annotations(blog, count_queries):
    api = BoltAPI()

    @api.get("/authors")
    async def list_authors() -> list[AuthorWithPostCount]:
        return Author.objects.all()

    with TestClient(api) as client:
        response = client.get("/authors")

    assert response.status_code == 200
    assert response.json() == [{"id": blog[0].id, "name": "Alice", "post_count": 3}]
    assert len(count_queries) == 1


@pytest.mark.django_db(transaction=True)
def test_paginated_route_applies_plan_to_the_page(blog, count_queries):
    class TwoPerPage(PageNumberPagination):
        page_size = 2

    api = BoltAPI()

    @api.get("/posts")
    @paginate(TwoPerPage)
    async def list_posts(request) -> list[PostFull]:
        return BlogPost.objects.order_by("id")

    with TestClient(api) as client:
        response = client.get("/posts?page=1")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [p["title"] for p in items] == ["Post 0", "Post 1"]
    assert items[0]["comments"][0]["author"]["name"] == "Alice"
    # count + page(posts+author) + tags + comments
    assert len(count_queries) == 4


# ---------------------------------------------------------------------------
# Django 6.1: fetch_mode(FETCH_PEERS) as the safety net for anything the plan
# does not cover (deferred fields, relations behind properties)
# ---------------------------------------------------------------------------


@needs_fetch_modes
def test_plan_sets_fetch_peers_when_available():
    qs = PostWithAuthor.loading_plan(BlogPost).apply(BlogPost.objects.all())
    assert qs._fetch_mode is fetch_modes.FETCH_PEERS


@needs_fetch_modes
def test_plan_keeps_callers_explicit_fetch_mode():
    qs = BlogPost.objects.fetch_mode(fetch_modes.FETCH_RAISE)
    assert PostWithAuthor.loading_plan(BlogPost).apply(qs)._fetch_mode is fetch_modes.FETCH_RAISE


@needs_fetch_modes
@pytest.mark.django_db
def test_fetch_peers_batches_relations_the_plan_cannot_see(blog, django_assert_num_queries):
    class PostWithAuthorViaProperty(Serializer):
        id: int
        author_name: str = field(source="author_display")  # a model property, opaque to the plan

    BlogPost.author_display = property(lambda self: self.author.name)
    try:
        assert not PostWithAuthorViaProperty.loading_plan(BlogPost).select_related
        # posts + ONE peers batch for author (not one query per post)
        with django_assert_num_queries(2):
            dumped = PostWithAuthorViaProperty.dump_many(
                PostWithAuthorViaProperty.from_models(BlogPost.objects.order_by("id"))
            )
    finally:
        del BlogPost.author_display
    assert [d["author_name"] for d in dumped] == ["Alice"] * 3
