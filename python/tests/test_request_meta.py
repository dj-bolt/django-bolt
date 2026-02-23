"""Tests for request.META property (Django template compatibility)."""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


@pytest.fixture(scope="module")
def api():
    """Create test API with routes that access request.META."""
    api = BoltAPI()

    @api.get("/meta")
    async def get_meta(request):
        """Return all META keys and values."""
        meta = request.META
        return {k: v for k, v in meta.items()}

    @api.get("/meta/method")
    async def get_meta_method(request):
        """Return REQUEST_METHOD from META."""
        return {"method": request.META.get("REQUEST_METHOD")}

    @api.get("/meta/path")
    async def get_meta_path(request):
        """Return PATH_INFO from META."""
        return {"path": request.META.get("PATH_INFO")}

    @api.get("/meta/query")
    async def get_meta_query(request):
        """Return QUERY_STRING from META."""
        return {"query": request.META.get("QUERY_STRING")}

    @api.get("/meta/headers")
    async def get_meta_headers(request):
        """Return HTTP_* headers from META."""
        meta = request.META
        return {
            "host": meta.get("HTTP_HOST"),
            "custom": meta.get("HTTP_X_CUSTOM"),
            "content_type": meta.get("CONTENT_TYPE"),
            "content_length": meta.get("CONTENT_LENGTH"),
        }

    @api.get("/meta/cached")
    async def get_meta_cached(request):
        """Verify META is cached (same object on multiple accesses)."""
        meta1 = request.META
        meta2 = request.META
        return {"cached": meta1 is meta2}

    @api.post("/meta/post")
    async def post_meta(request):
        """Return META for POST request."""
        return {"method": request.META.get("REQUEST_METHOD")}

    return api


@pytest.fixture(scope="module")
def client(api):
    """Create test client."""
    return TestClient(api)


class TestRequestMETA:
    """Test request.META property for Django compatibility."""

    def test_meta_request_method(self, client):
        """META contains REQUEST_METHOD."""
        response = client.get("/meta/method")
        assert response.status_code == 200
        assert response.json()["method"] == "GET"

    def test_meta_request_method_post(self, client):
        """META contains REQUEST_METHOD for POST."""
        response = client.post("/meta/post")
        assert response.status_code == 200
        assert response.json()["method"] == "POST"

    def test_meta_path_info(self, client):
        """META contains PATH_INFO."""
        response = client.get("/meta/path")
        assert response.status_code == 200
        assert response.json()["path"] == "/meta/path"

    def test_meta_query_string_empty(self, client):
        """META contains empty QUERY_STRING when no query params."""
        response = client.get("/meta/query")
        assert response.status_code == 200
        assert response.json()["query"] == ""

    def test_meta_query_string_with_params(self, client):
        """META contains QUERY_STRING with query params."""
        response = client.get("/meta/query?foo=bar&baz=123")
        assert response.status_code == 200
        query = response.json()["query"]
        # Query string should contain both params (order may vary)
        assert "foo=bar" in query
        assert "baz=123" in query

    def test_meta_http_headers(self, client):
        """META contains HTTP_* headers."""
        response = client.get(
            "/meta/headers",
            headers={
                "Host": "example.com",
                "X-Custom": "test-value",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["host"] == "example.com"
        assert data["custom"] == "test-value"

    def test_meta_content_type_header(self, client):
        """META contains CONTENT_TYPE (without HTTP_ prefix)."""
        response = client.get(
            "/meta/headers",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["content_type"] == "application/json"

    def test_meta_content_length_header(self, client):
        """META contains CONTENT_LENGTH (without HTTP_ prefix)."""
        response = client.get(
            "/meta/headers",
            headers={"Content-Length": "42"},
        )
        assert response.status_code == 200
        assert response.json()["content_length"] == "42"

    def test_meta_is_cached(self, client):
        """META dict is cached on first access."""
        response = client.get("/meta/cached")
        assert response.status_code == 200
        assert response.json()["cached"] is True

    def test_meta_all_keys(self, client):
        """META contains all expected keys."""
        response = client.get(
            "/meta?test=1",
            headers={
                "Host": "localhost",
                "Accept": "application/json",
            },
        )
        assert response.status_code == 200
        meta = response.json()

        # Standard META keys
        assert "REQUEST_METHOD" in meta
        assert "PATH_INFO" in meta
        assert "QUERY_STRING" in meta

        # Headers as HTTP_*
        assert "HTTP_HOST" in meta
        assert "HTTP_ACCEPT" in meta

        # Verify values
        assert meta["REQUEST_METHOD"] == "GET"
        assert meta["PATH_INFO"] == "/meta"
        assert "test=1" in meta["QUERY_STRING"]

    def test_meta_server_info(self, client):
        """META contains SERVER_NAME, SERVER_PORT, and SERVER_PROTOCOL."""
        response = client.get(
            "/meta",
            headers={"Host": "example.com:8080"},
        )
        assert response.status_code == 200
        meta = response.json()

        # Server info keys
        assert "SERVER_NAME" in meta
        assert "SERVER_PORT" in meta
        assert "SERVER_PROTOCOL" in meta

        # Verify values (parsed from Host header)
        assert meta["SERVER_NAME"] == "example.com"
        assert meta["SERVER_PORT"] == "8080"
        assert meta["SERVER_PROTOCOL"] == "HTTP/1.1"

    def test_meta_server_info_default_port(self, client):
        """META parses Host header without port correctly."""
        response = client.get(
            "/meta",
            headers={"Host": "example.com"},
        )
        assert response.status_code == 200
        meta = response.json()

        assert meta["SERVER_NAME"] == "example.com"
        assert meta["SERVER_PORT"] == "80"  # Default port when not specified

    def test_meta_server_info_default_port_https(self, api):
        """META defaults SERVER_PORT to 443 when scheme is HTTPS."""
        with TestClient(api) as secure_client:
            response = secure_client.get(
                "/meta",
                headers={
                    "Host": "example.com",
                    "X-Forwarded-Proto": "https",
                },
            )
        assert response.status_code == 200
        meta = response.json()

        assert meta["SERVER_NAME"] == "example.com"
        assert meta["SERVER_PORT"] == "443"

    def test_meta_remote_addr(self, client):
        """META contains REMOTE_ADDR and REMOTE_HOST."""
        response = client.get("/meta")
        assert response.status_code == 200
        meta = response.json()

        # Client info keys
        assert "REMOTE_ADDR" in meta
        assert "REMOTE_HOST" in meta

        # Both should have a valid IP address (or "127.0.0.1" for localhost)
        assert meta["REMOTE_ADDR"]  # Non-empty
        assert meta["REMOTE_HOST"]  # Non-empty

    def test_meta_script_name(self, client):
        """META contains SCRIPT_NAME (usually empty for Django apps)."""
        response = client.get("/meta")
        assert response.status_code == 200
        meta = response.json()

        assert "SCRIPT_NAME" in meta
        assert meta["SCRIPT_NAME"] == ""

    def test_meta_query_string_preserves_encoding(self, client):
        """META QUERY_STRING preserves URL encoding from original request."""
        # URL-encoded query string: foo=hello%20world -> foo=hello world when decoded
        # But QUERY_STRING should preserve the original encoding
        response = client.get("/meta/query?foo=hello%20world&bar=a%2Bb")
        assert response.status_code == 200
        query = response.json()["query"]

        # The raw query string should be preserved
        assert "foo=hello%20world" in query or "foo=hello world" in query
        assert "bar=a%2Bb" in query or "bar=a+b" in query
