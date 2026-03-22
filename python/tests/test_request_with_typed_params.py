"""Tests for request.headers/body/query when handler has typed params alongside request.

Regression tests for: handlers with both a `request` parameter and other
typed parameters (path, query, body) must still have request.headers,
request.body, and request.query populated. The static analysis optimization
that skips unused parsing must not skip components accessible via the
request object.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI
from django_bolt.testing import AsyncTestClient, TestClient


@pytest.fixture(scope="module")
def api():
    """Create test API with routes that have request + typed params."""
    api = BoltAPI()

    # Handler with request + path param — request.headers should still work
    @api.get("/items/{item_id}/headers")
    async def get_headers_with_path_param(request, item_id: str):
        return {
            "item_id": item_id,
            "headers": dict(request.headers),
        }

    # Handler with request + query param — request.headers should still work
    @api.get("/search/headers")
    async def get_headers_with_query_param(request, q: str = ""):
        return {
            "q": q,
            "headers": dict(request.headers),
        }

    # Handler with request + path param — request.body should still work
    @api.post("/items/{item_id}/body")
    async def get_body_with_path_param(request, item_id: str):
        return {
            "item_id": item_id,
            "body": request.body.decode(),
        }

    # Handler with request + query param — request.body should still work
    @api.post("/echo/body")
    async def get_body_with_query_param(request, format: str = "raw"):
        return {
            "format": format,
            "body": request.body.decode(),
        }

    # Handler with request + path param — request.query should still work
    @api.get("/items/{item_id}/query")
    async def get_query_with_path_param(request, item_id: str):
        return {
            "item_id": item_id,
            "query": dict(request.query),
        }

    # Handler with request + path + query — all should work
    @api.post("/items/{item_id}/all")
    async def get_all_with_mixed_params(request, item_id: str, token: str = ""):
        return {
            "item_id": item_id,
            "token": token,
            "headers": dict(request.headers),
            "body": request.body.decode(),
            "query": dict(request.query),
        }

    # Handler with request + path param — request.cookies should still work
    @api.get("/items/{item_id}/cookies")
    async def get_cookies_with_path_param(request, item_id: str):
        return {
            "item_id": item_id,
            "cookies": dict(request.cookies),
        }

    # Control: request-only handler — should always work
    @api.get("/headers-only")
    async def get_headers_only(request):
        return {"headers": dict(request.headers)}

    return api


@pytest.fixture(scope="module")
def client(api):
    return TestClient(api)


class TestRequestHeadersWithTypedParams:
    """request.headers must be populated when handler has other typed params."""

    def test_headers_with_path_param(self, client):
        """request.headers works when handler has a path param."""
        response = client.get(
            "/items/abc/headers",
            headers={"X-Custom": "test-value", "Authorization": "Bearer tok"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["item_id"] == "abc"
        assert data["headers"].get("x-custom") == "test-value"
        assert data["headers"].get("authorization") == "Bearer tok"

    def test_headers_with_query_param(self, client):
        """request.headers works when handler has a query param."""
        response = client.get(
            "/search/headers?q=test",
            headers={"X-Request-Id": "req-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["q"] == "test"
        assert data["headers"].get("x-request-id") == "req-123"

    def test_headers_request_only_control(self, client):
        """Control: request-only handler always has headers (no regression)."""
        response = client.get(
            "/headers-only",
            headers={"X-Control": "yes"},
        )
        assert response.status_code == 200
        assert response.json()["headers"].get("x-control") == "yes"


class TestRequestBodyWithTypedParams:
    """request.body must be populated when handler has other typed params."""

    def test_body_with_path_param(self, client):
        """request.body works when handler has a path param."""
        response = client.post(
            "/items/xyz/body",
            content=b'{"key": "value"}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["item_id"] == "xyz"
        assert data["body"] == '{"key": "value"}'

    def test_body_with_query_param(self, client):
        """request.body works when handler has a query param."""
        response = client.post(
            "/echo/body?format=json",
            content=b"hello world",
        )
        assert response.status_code == 200
        data = response.json()
        assert data["format"] == "json"
        assert data["body"] == "hello world"


class TestRequestQueryWithTypedParams:
    """request.query must be populated when handler has other typed params."""

    def test_query_with_path_param(self, client):
        """request.query works when handler has a path param."""
        response = client.get("/items/123/query?page=2&limit=10")
        assert response.status_code == 200
        data = response.json()
        assert data["item_id"] == "123"
        assert str(data["query"]["page"]) == "2"
        assert str(data["query"]["limit"]) == "10"


class TestRequestCookiesWithTypedParams:
    """request.cookies must be populated when handler has other typed params."""

    def test_cookies_with_path_param(self, client):
        """request.cookies works when handler has a path param."""
        response = client.get(
            "/items/abc/cookies",
            headers={"Cookie": "session=abc123; theme=dark"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["item_id"] == "abc"
        assert data["cookies"].get("session") == "abc123"
        assert data["cookies"].get("theme") == "dark"


class TestAllComponentsWithMixedParams:
    """All request components accessible when handler has mixed param types."""

    def test_all_components_populated(self, client):
        """headers, body, query, and path params all work together."""
        response = client.post(
            "/items/item-42/all?token=hmac-abc",
            content=b'{"action": "process"}',
            headers={
                "Content-Type": "application/json",
                "X-Task-Token": "secret-token",
            },
        )
        assert response.status_code == 200
        data = response.json()

        # Path param extracted correctly
        assert data["item_id"] == "item-42"

        # Query param extracted correctly (both via typed param and request.query)
        assert data["token"] == "hmac-abc"
        assert data["query"].get("token") == "hmac-abc"

        # Headers populated
        assert data["headers"].get("x-task-token") == "secret-token"
        assert data["headers"].get("content-type") == "application/json"

        # Body populated
        assert data["body"] == '{"action": "process"}'
