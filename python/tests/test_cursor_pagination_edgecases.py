"""Cursor ordering edge cases: ties, legacy cursors, timestamps, and values()."""

import base64
import json
from urllib.parse import quote

import pytest
from django.db.models import Case, JSONField, Value, When
from django.utils import timezone

from django_bolt import BoltAPI, CursorPagination, paginate
from django_bolt.testing import TestClient

from .test_models import Article

PREFIX = "cursor-edge-"


def _seed():
    Article.objects.bulk_create(
        [
            Article(
                title=f"{PREFIX}{i:02d}",
                content="cursor correctness probe",
                author="A" if i < 12 else "B",
            )
            for i in range(18)
        ]
    )


def _seed_interleaved():
    Article.objects.bulk_create(
        [
            Article(
                title=f"{PREFIX}{i:02d}",
                content="interleaved cursor probe",
                author="A" if i % 2 == 0 else "B",
            )
            for i in range(18)
        ]
    )


def _assert_author_order(found, authors_by_id, ordering):
    authors = [authors_by_id[item_id] for item_id in found]
    assert authors == sorted(authors, reverse=ordering.startswith("-"))


def _api(ordering, projection="model", page_size=3):
    api = BoltAPI()

    class ProbePagination(CursorPagination):
        pass

    ProbePagination.ordering = ordering
    ProbePagination.page_size = page_size

    @api.get("/cursor-edge")
    @paginate(ProbePagination)
    async def list_articles(request):
        qs = Article.objects.filter(title__startswith=PREFIX)
        if projection == "values_no_pk":
            return qs.values("title", "author")
        if projection == "values_with_pk":
            return qs.values("id", "title", "author")
        return qs

    return api


def _walk(client, *, key, max_pages=30):
    seen = []
    cursor = None
    for _ in range(max_pages):
        url = "/cursor-edge"
        if cursor is not None:
            url += f"?cursor={quote(cursor, safe='')}"
        response = client.get(url)
        assert response.status_code == 200, response.text
        data = response.json()
        seen.extend(row[key] for row in data["items"])
        if not data["has_next"]:
            assert data.get("next_cursor") is None
            return seen
        cursor = data.get("next_cursor")
        assert cursor, "has_next=True but next_cursor is missing"
    pytest.fail("Pagination did not terminate")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ordering", ["author", "-author"])
def test_duplicate_ordering_models_complete(ordering):
    _seed()
    authors_by_id = dict(Article.objects.filter(title__startswith=PREFIX).values_list("id", "author"))
    with TestClient(_api(ordering)) as client:
        found = _walk(client, key="id")
    assert len(found) == len(authors_by_id)
    assert set(found) == set(authors_by_id)
    _assert_author_order(found, authors_by_id, ordering)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ordering", ["author", "-author"])
def test_interleaved_author_ordering(ordering):
    _seed_interleaved()
    authors_by_id = dict(Article.objects.filter(title__startswith=PREFIX).values_list("id", "author"))
    with TestClient(_api(ordering)) as client:
        found = _walk(client, key="id")
    assert len(found) == len(authors_by_id)
    assert set(found) == set(authors_by_id)
    _assert_author_order(found, authors_by_id, ordering)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("projection", ["values_no_pk", "values_with_pk"])
def test_values_projection_complete(projection):
    _seed()
    expected = {f"{PREFIX}{i:02d}" for i in range(18)}
    with TestClient(_api("author", projection)) as client:
        found = _walk(client, key="title")
        first = client.get("/cursor-edge").json()["items"]
    assert len(found) == len(expected)
    assert set(found) == expected
    expected_fields = {"title", "author"} if projection == "values_no_pk" else {"id", "title", "author"}
    assert all(set(row) == expected_fields for row in first), "Projection leaked or lost fields"


@pytest.mark.django_db(transaction=True)
def test_default_pk_cursor_wire_format_unchanged():
    _seed()
    with TestClient(_api("-id")) as client:
        response = client.get("/cursor-edge")
    assert response.status_code == 200
    cursor = response.json()["next_cursor"]
    payload = json.loads(base64.b64decode(cursor))
    assert set(payload) == {"v"}
    assert isinstance(payload["v"], int), payload


@pytest.mark.django_db(transaction=True)
def test_legacy_nonunique_cursor_still_accepted():
    _seed()
    legacy = base64.b64encode(json.dumps({"v": "A"}).encode()).decode()
    with TestClient(_api("author")) as client:
        response = client.get("/cursor-edge?cursor=" + quote(legacy, safe=""))
    assert response.status_code == 200
    assert response.json()["items"][0]["author"] == "B"


@pytest.mark.django_db(transaction=True)
def test_datetime_ties_complete():
    _seed()
    Article.objects.filter(title__startswith=PREFIX).update(created_at=timezone.now())
    expected = set(Article.objects.filter(title__startswith=PREFIX).values_list("id", flat=True))
    with TestClient(_api("-created_at")) as client:
        found = _walk(client, key="id")
    assert len(found) == len(expected)
    assert set(found) == expected


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("ordering", "invalid_value"),
    [
        ("-created_at", "not-a-datetime"),
        ("is_published", {"invalid": "boolean"}),
    ],
)
def test_invalid_composite_ordering_value_resets_to_first_page(ordering, invalid_value):
    _seed()
    cursor = CursorPagination()._encode_cursor({"value": invalid_value, "pk": 1}, kind="composite-v2")
    with TestClient(_api(ordering)) as client:
        first = client.get("/cursor-edge")
        malformed = client.get("/cursor-edge?cursor=" + quote(cursor, safe=""))
    assert first.status_code == malformed.status_code == 200
    assert malformed.json()["items"] == first.json()["items"]
    assert malformed.json()["has_previous"] is False
    assert malformed.json()["next_cursor"] == first.json()["next_cursor"]


@pytest.mark.django_db(transaction=True)
def test_invalid_composite_pk_resets_to_first_page():
    _seed()
    cursor = CursorPagination()._encode_cursor({"value": "A", "pk": "invalid-pk"}, kind="composite-v2")
    with TestClient(_api("author")) as client:
        first = client.get("/cursor-edge")
        malformed = client.get("/cursor-edge?cursor=" + quote(cursor, safe=""))
    assert first.status_code == malformed.status_code == 200
    assert first.json()["items"] == malformed.json()["items"]
    assert malformed.json()["has_previous"] is False


def test_legacy_json_values_are_not_interpreted_as_structural_cursors():
    pagination = CursorPagination()
    legacy_json = {"value": "A", "pk": 7}
    json_null = {"null": True}
    assert pagination._decode_cursor(pagination._encode_cursor(legacy_json)) == legacy_json
    assert pagination._decode_cursor(pagination._encode_cursor(json_null)) == json_null
    tagged = pagination._decode_cursor(pagination._encode_cursor({"value": "A", "pk": 7}, kind="composite-v2"))
    assert tagged != legacy_json
    null_tagged = pagination._decode_cursor(pagination._encode_cursor(None, kind="null-v2"))
    assert null_tagged is not None and null_tagged != json_null


@pytest.mark.django_db(transaction=True)
def test_legacy_json_object_cursor_uses_scalar_filter():
    _seed()
    boundary = {"value": "A", "pk": 7}
    json_sort = Case(
        When(author="A", then=Value(boundary, output_field=JSONField())),
        default=Value({"value": "B", "pk": 8}, output_field=JSONField()),
        output_field=JSONField(),
    )
    queryset = Article.objects.filter(title__startswith=PREFIX).annotate(json_sort=json_sort)
    expected_ids = list(
        queryset.filter(json_sort__gt=boundary).order_by("json_sort", "pk").values_list("id", flat=True)[:2]
    )
    assert len(expected_ids) == 2

    api = BoltAPI()

    class Page(CursorPagination):
        ordering = "json_sort"
        page_size = 2

    @api.get("/cursor-json-legacy")
    @paginate(Page)
    async def list_articles(request):
        return Article.objects.filter(title__startswith=PREFIX).annotate(json_sort=json_sort)

    legacy = base64.b64encode(json.dumps({"v": boundary}).encode()).decode()
    with TestClient(api) as client:
        response = client.get("/cursor-json-legacy?cursor=" + quote(legacy, safe=""))
    assert response.status_code == 200, response.text
    data = response.json()
    assert [row["id"] for row in data["items"]] == expected_ids
    assert data["has_next"] is True
    assert data["next_cursor"] is not None


@pytest.mark.django_db(transaction=True)
def test_composite_cursor_on_pk_ordering_starts_from_first_page():
    _seed()
    api = BoltAPI()

    class ByPK(CursorPagination):
        ordering = "-id"
        page_size = 2

    @api.get("/cursor-pk-only")
    @paginate(ByPK)
    async def articles(request):
        return Article.objects.filter(title__startswith=PREFIX)

    bogus = ByPK()._encode_cursor({"value": 1, "pk": 1}, kind="composite-v2")
    with TestClient(api) as client:
        expected = client.get("/cursor-pk-only")
        response = client.get("/cursor-pk-only?cursor=" + quote(bogus, safe=""))
    assert response.status_code == 200, response.text
    assert response.json()["items"] == expected.json()["items"]
    assert response.json()["has_next"] is True


@pytest.mark.django_db(transaction=True)
def test_union_values_first_page_retains_legacy_support():
    """UNION supports first-page slicing; Django rejects filter() on later pages."""
    _seed()
    api = BoltAPI()

    class Page(CursorPagination):
        ordering = "author"
        page_size = 1

    @api.get("/cursor-union")
    @paginate(Page)
    async def list_articles(request):
        # Article.Meta.ordering applies inside UNION unless explicitly cleared.
        # SQLite rejects ORDER BY in compound-query operands.
        items = Article.objects.filter(author="A", title__startswith=PREFIX).order_by().values("author")
        others = Article.objects.filter(author="B", title__startswith=PREFIX).order_by().values("author")
        return items.union(others)

    with TestClient(api) as client:
        response = client.get("/cursor-union")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [{"author": "A"}]
    assert response.json()["has_next"] is True
    # The legacy response emits a cursor, but its continuation is not supported:
    # Django does not allow filter() on a queryset after union().
    assert response.json()["next_cursor"] is not None
