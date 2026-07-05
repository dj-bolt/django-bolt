"""App under test for the HTTP QUERY method."""

from __future__ import annotations

import msgspec

from django_bolt import BoltAPI

api = BoltAPI()


class SearchQuery(msgspec.Struct):
    term: str
    limit: int = 10


@api.query("/search")
async def search(query: SearchQuery) -> dict:
    return {"term": query.term, "limit": query.limit}
