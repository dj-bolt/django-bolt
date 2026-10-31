"""Regression tests for cursor ordering ties and projected querysets."""

from urllib.parse import quote

import pytest

from django_bolt import BoltAPI, CursorPagination, paginate
from django_bolt.testing import TestClient

from .test_models import Article


def _make_cursor_case(size):
    """Build a dataset with deliberately duplicated ordering values."""

    Article.objects.bulk_create(
        [
            Article(
                title=f"cursor-regression-{i:03d}",
                content="Regression test",
                author="A" if i < 64 else "B",
            )
            for i in range(96)
        ]
    )

    expected_ids = set(Article.objects.filter(title__startswith="cursor-regression-").values_list("id", flat=True))

    api = BoltAPI()

    class CursorByAuthor(CursorPagination):
        page_size = size
        ordering = "author"

    @api.get("/cursor-regression")
    @paginate(CursorByAuthor)
    async def list_articles(request):
        return Article.objects.filter(title__startswith="cursor-regression-")

    return api, expected_ids


@pytest.mark.parametrize("page_size", [8, 7])
@pytest.mark.django_db(transaction=True)
def test_cursor_nonunique_ordering_preserves_all_rows(page_size):
    api, expected_ids = _make_cursor_case(page_size)

    seen_ids = []
    cursor = None

    with TestClient(api) as client:
        for _ in range(30):
            url = "/cursor-regression"

            if cursor is not None:
                url += f"?cursor={quote(cursor, safe='')}"

            response = client.get(url)
            assert response.status_code == 200

            data = response.json()
            seen_ids.extend(item["id"] for item in data["items"])

            cursor = data.get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail("Pagination did not terminate within 30 pages")

    missing = expected_ids - set(seen_ids)
    duplicates = len(seen_ids) - len(set(seen_ids))

    assert not missing, f"Missing IDs: {sorted(missing)[:20]}"
    assert duplicates == 0
    assert set(seen_ids) == expected_ids


@pytest.mark.django_db(transaction=True)
def test_cursor_values_without_pk_preserves_all_rows():
    Article.objects.bulk_create(
        [
            Article(
                title=f"values-cursor-{i:02d}",
                content="Cursor regression",
                author="A" if i < 12 else "B",
            )
            for i in range(18)
        ]
    )

    expected = {f"values-cursor-{i:02d}" for i in range(18)}

    api = BoltAPI()

    class ValuesCursorPagination(CursorPagination):
        page_size = 3
        ordering = "author"

    @api.get("/cursor-values")
    @paginate(ValuesCursorPagination)
    async def list_values(request):
        return Article.objects.filter(title__startswith="values-cursor-").values("title", "author")

    seen = []
    cursor = None

    with TestClient(api) as client:
        for _ in range(15):
            url = "/cursor-values"
            if cursor is not None:
                url += f"?cursor={quote(cursor, safe='')}"

            response = client.get(url)
            assert response.status_code == 200

            data = response.json()
            assert all(set(item) == {"title", "author"} for item in data["items"])
            seen.extend(item["title"] for item in data["items"])

            cursor = data.get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail("Cursor pagination did not terminate")

    assert len(seen) == len(expected)
    assert set(seen) == expected
    assert len(seen) == len(set(seen))
