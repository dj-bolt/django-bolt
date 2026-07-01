"""Tests that Depends() targets participate in request-component detection.

Regression tests for: when a handler's Depends() target reads request.query,
request.headers, request.body, or request.cookies, the handler should still
have those request components populated — even if the handler itself does not
reference them directly.

Before the fix, static AST analysis only walked the handler body. A dependency
that read `request.query` was invisible to the analyzer, so Rust skipped query
parsing for that route and the dep saw an empty dict.
"""

from __future__ import annotations

from typing import Annotated

import pytest

from django_bolt import BoltAPI
from django_bolt.params import Depends, Query
from django_bolt.testing import TestClient

# --- Dependency callables (module-level so inspect.getsource works) ---


def dep_reads_query(request):
    """Dep that reads request.query only."""
    return dict(request.query)


def dep_reads_headers(request):
    """Dep that reads request.headers only."""
    return dict(request.headers)


def dep_reads_cookies(request):
    """Dep that reads request.cookies only."""
    return dict(request.cookies)


def dep_reads_body(request):
    """Dep that reads request.body only."""
    return request.body.decode() if request.body else ""


def dep_reads_query_req_alias(req):
    """Dep that uses alias 'req' instead of 'request'."""
    return dict(req.query)


class FilterBackend:
    """Class-callable dep (the BoltFilterBackend pattern)."""

    def __init__(self, fields):
        self.fields = fields

    def __call__(self, request):
        q = request.query
        return {name: q.get(name) for name in self.fields}


def dep_with_typed_query_param(page: Annotated[str, Query()] = "1"):
    """Dep that declares query via typed param (baseline — already works).

    Uses ``str`` because the current dep value extractor does not type-coerce
    query values (pre-existing limitation tracked separately).
    """
    return {"page": page}


# --- Fixtures ---


@pytest.fixture(scope="module")
def api():
    api = BoltAPI()

    # Handler with Depends that reads request.query — handler itself ignores request
    @api.get("/filter-query")
    async def filter_query(
        filters: Annotated[dict, Depends(dep_reads_query)],
    ):
        return {"filters": filters}

    # Handler with Depends reading headers
    @api.get("/dep-headers")
    async def dep_headers(
        hdrs: Annotated[dict, Depends(dep_reads_headers)],
    ):
        return {"headers": hdrs}

    # Handler with Depends reading cookies
    @api.get("/dep-cookies")
    async def dep_cookies(
        cookies: Annotated[dict, Depends(dep_reads_cookies)],
    ):
        return {"cookies": cookies}

    # Handler with Depends reading body
    @api.post("/dep-body")
    async def dep_body(
        body: Annotated[str, Depends(dep_reads_body)],
    ):
        return {"body": body}

    # Dep with 'req' alias instead of 'request'
    @api.get("/dep-req-alias")
    async def dep_req_alias(
        filters: Annotated[dict, Depends(dep_reads_query_req_alias)],
    ):
        return {"filters": filters}

    # Class-callable dep (BoltFilterBackend pattern from the issue)
    @api.get("/users")
    async def list_users(
        filters: Annotated[dict, Depends(FilterBackend(fields=("pk", "is_active")))],
    ):
        return {"filters": filters}

    # Baseline control: typed Query() in the dep — already works today
    @api.get("/typed-dep")
    async def typed_dep(
        pagination: Annotated[dict, Depends(dep_with_typed_query_param)],
    ):
        return pagination

    return api


@pytest.fixture(scope="module")
def client(api):
    return TestClient(api)


# --- Tests ---


class TestDependsReadsRequestQuery:
    def test_function_dep_reads_query(self, client):
        """Dep that reads request.query sees the parsed params."""
        response = client.get("/filter-query?is_active=false&pk=42")
        assert response.status_code == 200
        assert response.json() == {
            "filters": {"is_active": "false", "pk": "42"},
        }

    def test_function_dep_reads_query_empty(self, client):
        """Dep still works when there are no query params."""
        response = client.get("/filter-query")
        assert response.status_code == 200
        assert response.json() == {"filters": {}}

    def test_dep_with_req_alias(self, client):
        """Dep using 'req' alias still has query populated."""
        response = client.get("/dep-req-alias?x=1")
        assert response.status_code == 200
        assert response.json() == {"filters": {"x": "1"}}

    def test_class_callable_dep_reads_query(self, client):
        """Class-callable dep (__call__) sees request.query."""
        response = client.get("/users?is_active=false&pk=7&other=ignored")
        assert response.status_code == 200
        body = response.json()
        assert body == {"filters": {"pk": "7", "is_active": "false"}}

    def test_typed_query_in_dep_still_works(self, client):
        """Control: typed Query() in dep already works — must not regress."""
        response = client.get("/typed-dep?page=3")
        assert response.status_code == 200
        assert response.json() == {"page": "3"}


class TestDependsReadsOtherRequestComponents:
    def test_dep_reads_headers(self, client):
        response = client.get(
            "/dep-headers",
            headers={"X-Custom-Thing": "abc"},
        )
        assert response.status_code == 200
        assert response.json()["headers"].get("x-custom-thing") == "abc"

    def test_dep_reads_cookies(self, client):
        response = client.get(
            "/dep-cookies",
            headers={"Cookie": "session=xyz; theme=dark"},
        )
        assert response.status_code == 200
        cookies = response.json()["cookies"]
        assert cookies.get("session") == "xyz"
        assert cookies.get("theme") == "dark"

    def test_dep_reads_body(self, client):
        response = client.post(
            "/dep-body",
            content=b'{"key":"value"}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json() == {"body": '{"key":"value"}'}
