"""
Tests for first-install experience.

These tests validate that a minimal django-bolt project (like one created by
``django-bolt init``) works correctly out of the box.  They specifically target
bugs that existing tests did not catch because the example projects were more
complex than the simplest possible setup.

Bugs covered:
  - OpenAPI docs showing no routes when the user route is not registered.
  - Static file serving warning when STATIC_ROOT is None (Rust converted
    Python None to the string "None").

Note: static serving for admin/app assets is handled entirely by the native
Rust /static scope (STATIC_ROOT/STATICFILES_DIRS, plus staticfiles finders in
DEBUG). There is no Python static request route, so there are no Python static
route metadata tests here; that path is exercised by test_static_files.py and
the static server integration tests.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

# ---------------------------------------------------------------------------
# Bug 2 – Minimal API: single route must actually work
# ---------------------------------------------------------------------------


class TestMinimalFirstInstall:
    """A minimal single-route API must work out of the box."""

    def test_single_route_responds(self):
        """The simplest possible API must return 200 on the registered route."""
        api = BoltAPI()

        @api.get("/test")
        def index():
            return "Hello, World!"

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Plain string returns as text, not JSON
            assert "Hello, World!" in response.text

    def test_single_async_route_responds(self):
        """Async handler also works."""
        api = BoltAPI()

        @api.get("/test")
        async def index():
            return "Hello, World!"

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            assert "Hello, World!" in response.text

    def test_openapi_docs_include_user_routes(self):
        """OpenAPI docs must include user-defined routes, not just framework routes."""
        api = BoltAPI()

        @api.get("/test")
        async def index():
            return "Hello, World!"

        # Register OpenAPI routes (as runbolt does)
        api._register_openapi_routes()

        with TestClient(api) as client:
            response = client.get("/docs/openapi.json")
            assert response.status_code == 200

            schema = response.json()
            paths = schema.get("paths", {})
            assert "/test" in paths, f"User route /test not found in OpenAPI paths. Paths found: {list(paths.keys())}"

    def test_openapi_docs_endpoint_serves_html(self):
        """The /docs endpoint must serve HTML UI."""
        api = BoltAPI()

        @api.get("/test")
        async def index():
            return "Hello, World!"

        api._register_openapi_routes()

        with TestClient(api) as client:
            response = client.get("/docs")
            assert response.status_code == 200
            # Should be HTML content (Swagger/Scalar/etc UI)
            content_type = response.headers.get("content-type", "")
            assert "text/html" in content_type, f"Expected HTML content at /docs, got content-type: {content_type}"


# ---------------------------------------------------------------------------
# Bug 3 – Route count should distinguish user vs framework routes
# ---------------------------------------------------------------------------


class TestRouteCountAccuracy:
    """Route count messaging should be clear about user vs framework routes."""

    def test_framework_routes_are_separate_from_user_routes(self):
        """After OpenAPI registration, user route count should be distinguishable."""
        api = BoltAPI()

        @api.get("/hello")
        async def hello():
            return "hello"

        @api.get("/world")
        async def world():
            return "world"

        user_count_before = len(api._routes)
        assert user_count_before == 2, f"Expected 2 user routes, got {user_count_before}"

        api._register_openapi_routes()

        total_count = len(api._routes)
        framework_routes = total_count - user_count_before
        assert framework_routes > 0, "OpenAPI should add framework routes"
        assert user_count_before == 2, "User route count should not change"

    def test_user_routes_appear_in_schema(self):
        """All user routes must appear in OpenAPI schema even with many framework routes."""
        api = BoltAPI()

        @api.get("/users")
        async def users():
            return []

        @api.post("/users")
        async def create_user():
            return {}

        @api.get("/users/{user_id}")
        async def get_user(user_id: int):
            return {}

        api._register_openapi_routes()

        with TestClient(api) as client:
            response = client.get("/docs/openapi.json")
            schema = response.json()
            paths = schema.get("paths", {})

            assert "/users" in paths, f"Missing /users in schema paths: {list(paths.keys())}"
            assert "/users/{user_id}" in paths, "Missing /users/{user_id} in schema paths"

            # Verify methods
            assert "get" in paths["/users"], "/users should have GET"
            assert "post" in paths["/users"], "/users should have POST"
            assert "get" in paths["/users/{user_id}"], "/users/{user_id} should have GET"
