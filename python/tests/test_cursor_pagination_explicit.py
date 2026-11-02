"""Explicit ordering contracts for CursorPagination."""

import base64
import json
from urllib.parse import quote

import pytest
from django.db.models import Case, Count, IntegerField, Value, When

from django_bolt import BoltAPI, CursorPagination, paginate
from django_bolt.testing import TestClient

from .test_models import Article


PREFIX = "cursor-explicit-"


def _seed():
    Article.objects.bulk_create(
        [
            Article(
                title=f"{PREFIX}{i:03d}",
                content="cursor",
                author="A" if i < 12 else "B",
                is_published=i % 2 == 0,
            )
            for i in range(24)
        ]
    )


def _api(ordering, projection="model", page_size=3):
    api = BoltAPI()

    class Page(CursorPagination):
        pass

    Page.ordering = ordering
    Page.page_size = page_size

    @api.get("/cursor-explicit")
    @paginate(Page)
    async def entries(request):
        qs = Article.objects.filter(title__startswith=PREFIX)
        if projection == "distinct":
            return qs.values("author", "is_published").distinct()
        if projection in {"grouped", "aggregate"}:
            return qs.values("author", "is_published").annotate(total=Count("id"))
        if projection == "nullable":
            return qs.annotate(
                nullable_sort=Case(
                    When(author="A", then=Value(None)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
        if projection == "values":
            return qs.values("author", "id")
        if projection == "tuple":
            return qs.values_list("author", "id")
        return qs

    return api


def _walk(ordering, projection="model", page_size=3):
    results, cursor = [], None
    with TestClient(_api(ordering, projection, page_size)) as client:
        for _ in range(60):
            url = "/cursor-explicit" + (
                "?cursor=" + quote(cursor, safe="") if cursor is not None else ""
            )
            response = client.get(url)
            assert response.status_code == 200, response.text
            payload = response.json()
            results.extend(payload["items"])
            assert len(payload["items"]) <= page_size
            if not payload["has_next"]:
                assert payload.get("next_cursor") is None
                return results
            cursor = payload.get("next_cursor")
            assert cursor, "has_next without usable next_cursor"
    pytest.fail("Cursor did not terminate")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ordering", [("author", "id"), ("author", "-id"), ("-author", "-id")])
def test_multi_key_models_match_database_order(ordering):
    _seed()
    actual = [row["id"] for row in _walk(ordering)]
    expected = list(
        Article.objects.filter(title__startswith=PREFIX)
        .order_by(*ordering)
        .values_list("id", flat=True)
    )
    assert actual == expected


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("projection", ["distinct", "grouped"])
def test_multi_key_distinct_grouping_preserves_results(projection):
    _seed()
    rows = _walk(("author", "is_published"), projection, page_size=1)
    qs = Article.objects.filter(title__startswith=PREFIX).values("author", "is_published")
    qs = qs.distinct() if projection == "distinct" else qs.annotate(total=Count("id"))
    assert rows == list(qs.order_by("author", "is_published"))
    assert len(rows) == 4


@pytest.mark.django_db(transaction=True)
def test_aggregate_sort_uses_explicit_group_ties():
    _seed()
    rows = _walk(("total", "author", "is_published"), "aggregate", page_size=1)
    expected = list(
        Article.objects.filter(title__startswith=PREFIX)
        .values("author", "is_published")
        .annotate(total=Count("id"))
        .order_by("total", "author", "is_published")
    )
    assert rows == expected


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ordering", [("nullable_sort", "id"), ("-nullable_sort", "-id")])
def test_nullable_annotations_cover_every_row(ordering):
    _seed()
    rows = _walk(ordering, "nullable", page_size=2)
    expected = list(
        Article.objects.filter(title__startswith=PREFIX)
        .annotate(
            nullable_sort=Case(
                When(author="A", then=Value(None)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by(*ordering)
        .values_list("id", flat=True)
    )
    assert [r["id"] for r in rows] == expected


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("projection", ["values", "tuple"])
def test_explicit_projection_shape_unchanged(projection):
    _seed()
    rows = _walk(("author", "id"), projection, page_size=2)
    ids = [r["id"] if isinstance(r, dict) else r[1] for r in rows]
    assert len(ids) == len(set(ids)) == 24
    if projection == "values":
        assert all(set(r) == {"author", "id"} for r in rows)
    else:
        assert all(len(r) == 2 for r in rows)


@pytest.mark.django_db(transaction=True)
def test_unified_new_cursor_and_legacy_default_pk_cursor():
    _seed()
    with TestClient(_api(("author", "id"), page_size=1)) as client:
        new = client.get("/cursor-explicit").json()["next_cursor"]
    data = json.loads(base64.b64decode(new))
    assert data["t"] == "keyset-v1"
    assert len(data["v"]) == 2
    assert "j" not in data
    with TestClient(_api("-id", page_size=1)) as client:
        old = client.get("/cursor-explicit").json()["next_cursor"]
    legacy = json.loads(base64.b64decode(old))
    assert set(legacy) == {"v"}
    assert isinstance(legacy["v"], int)


@pytest.mark.django_db(transaction=True)
def test_explicit_single_column_does_not_append_pk():
    _seed()
    with TestClient(_api(("title",), page_size=1)) as client:
        cursor = client.get("/cursor-explicit").json()["next_cursor"]
    values = json.loads(base64.b64decode(cursor))
    assert values["t"] == "keyset-v1"
    assert len(values["v"]) == 1
    assert len(_walk(("title",))) == 24


@pytest.mark.django_db(transaction=True)
def test_default_pk_values_projection_uses_hidden_pk_without_leaking_it():
    _seed()
    api = BoltAPI()

    class Page(CursorPagination):
        ordering = "-id"
        page_size = 3

    @api.get("/hidden-pk")
    @paginate(Page)
    async def entries(request):
        return Article.objects.filter(title__startswith=PREFIX).values("title")

    cursor, titles = None, []
    with TestClient(api) as client:
        for _ in range(10):
            url = "/hidden-pk" + ("?cursor=" + quote(cursor, safe="") if cursor else "")
            response = client.get(url)
            assert response.status_code == 200, response.text
            body = response.json()
            assert all(set(row) == {"title"} for row in body["items"])
            titles.extend(row["title"] for row in body["items"])
            if not body["has_next"]:
                break
            cursor = body["next_cursor"]
            assert cursor
        else:
            pytest.fail("Default PK cursor did not terminate")

    assert len(titles) == len(set(titles)) == 24


@pytest.mark.django_db(transaction=True)
def test_values_pk_alias_is_used_for_automatic_tie_breaking():
    _seed()
    api = BoltAPI()

    class Page(CursorPagination):
        ordering = "author"
        page_size = 2

    @api.get("/alias-pk")
    @paginate(Page)
    async def entries(request):
        return Article.objects.filter(title__startswith=PREFIX).values("pk", "author")

    ids, cursor = [], None
    with TestClient(api) as client:
        for _ in range(20):
            url = "/alias-pk" + ("?cursor=" + quote(cursor, safe="") if cursor else "")
            response = client.get(url)
            assert response.status_code == 200, response.text
            body = response.json()
            assert all(set(row) == {"pk", "author"} for row in body["items"])
            ids.extend(row["pk"] for row in body["items"])
            if not body["has_next"]:
                break
            cursor = body["next_cursor"]
            assert cursor
        else:
            pytest.fail("PK alias cursor did not terminate")
    assert len(ids) == len(set(ids)) == 24
