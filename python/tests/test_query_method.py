"""Tests for the HTTP QUERY method."""

from __future__ import annotations

import msgspec

from django_bolt import BoltAPI, Router
from django_bolt.openapi.config import OpenAPIConfig
from django_bolt.testing import TestClient
from django_bolt.views import APIView


class SearchQuery(msgspec.Struct):
    term: str
    limit: int = 10


def test_query_with_json_body():
    api = BoltAPI()

    @api.query("/search")
    async def search(query: SearchQuery) -> dict:
        return {"term": query.term, "limit": query.limit}

    with TestClient(api) as client:
        response = client.query("/search", json={"term": "django", "limit": 5})
        assert response.status_code == 200, response.text
        assert response.json() == {"term": "django", "limit": 5}

        defaulted = client.query("/search", json={"term": "only"})
        assert defaulted.status_code == 200, defaulted.text
        assert defaulted.json() == {"term": "only", "limit": 10}


def test_query_and_get_coexist_on_same_path():
    api = BoltAPI()

    @api.get("/resource")
    async def read() -> dict:
        return {"method": "GET"}

    @api.query("/resource")
    async def find(query: SearchQuery) -> dict:
        return {"method": "QUERY", "term": query.term}

    with TestClient(api) as client:
        get_resp = client.get("/resource")
        assert get_resp.status_code == 200
        assert get_resp.json() == {"method": "GET"}

        query_resp = client.query("/resource", json={"term": "x"})
        assert query_resp.status_code == 200
        assert query_resp.json() == {"method": "QUERY", "term": "x"}

        response = client.options("/resource")
        assert response.status_code == 204
        allow = response.headers.get("allow", "")
        assert "QUERY" in allow, f"Allow header missing QUERY: {allow!r}"


def test_query_via_router():
    api = BoltAPI()
    router = Router(prefix="/v1")

    @router.query("/find")
    async def find(query: SearchQuery) -> dict:
        return {"term": query.term, "limit": query.limit}

    api.include_router(router)

    with TestClient(api) as client:
        response = client.query("/v1/find", json={"term": "routed", "limit": 3})
        assert response.status_code == 200, response.text
        assert response.json() == {"term": "routed", "limit": 3}


def test_query_openapi_operation():
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Q", version="1.0.0"))

    @api.query("/search")
    async def search(query: SearchQuery) -> dict:
        return {"term": query.term}

    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")
        assert response.status_code == 200, response.text
        schema = response.json()

    path_item = schema["paths"]["/search"]
    assert "query" in path_item, f"query operation missing: {list(path_item)}"
    operation = path_item["query"]
    assert "requestBody" in operation, "QUERY operation should document a request body"
    assert operation["operationId"] == "query_search"


def test_query_class_based_view():
    api = BoltAPI()

    @api.view("/cbv-search")
    class SearchView(APIView):
        async def query(self, query: SearchQuery) -> dict:
            return {"term": query.term, "limit": query.limit}

    with TestClient(api) as client:
        response = client.query("/cbv-search", json={"term": "cbv", "limit": 2})
        assert response.status_code == 200, response.text
        assert response.json() == {"term": "cbv", "limit": 2}


def test_query_missing_required_body_field_is_422():
    api = BoltAPI()

    @api.query("/search")
    async def search(query: SearchQuery) -> dict:
        return {"term": query.term}

    with TestClient(api) as client:
        response = client.query("/search", json={"limit": 5})
        assert response.status_code == 422, response.text
