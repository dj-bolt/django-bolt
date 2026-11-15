"""
Tests for API prefix and mount path behavior.

These tests verify:
1. Mount paths are normalized to include leading slash
2. OpenAPI docs remain at absolute paths regardless of API prefix
"""

import asyncio

import pytest

from django_bolt import BoltAPI, Router, WebSocket
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated
from django_bolt.middleware import Middleware
from django_bolt.openapi import OpenAPIConfig, SwaggerRenderPlugin
from django_bolt.testing import TestClient, WebSocketTestClient


class TestMountPathNormalization:
    """Tests for mount() path normalization."""

    def test_mount_without_leading_slash_normalizes_path(self):
        """Mount path without leading slash should be normalized to include it.

        When calling api.mount("mount", sub_api), the resulting routes should
        be registered as "/mount/..." not "mount/...".
        """
        main_api = BoltAPI()
        sub_api = BoltAPI()

        @sub_api.get("/endpoint")
        async def sub_endpoint():
            return {"source": "sub"}

        # Mount WITHOUT leading slash
        main_api.mount("mount", sub_api)

        # Check that route was registered with proper leading slash
        registered_paths = [path for _method, path, _handler_id, _handler in main_api._routes]
        assert "/mount/endpoint" in registered_paths, (
            f"Expected '/mount/endpoint' in routes, got: {registered_paths}. "
            "Mount path without leading slash should be normalized."
        )
        assert "mount/endpoint" not in registered_paths, (
            f"Invalid path 'mount/endpoint' (no leading slash) found in routes: {registered_paths}"
        )

    def test_mount_with_leading_slash_works(self):
        """Mount path with leading slash should work correctly."""
        main_api = BoltAPI()
        sub_api = BoltAPI()

        @sub_api.get("/endpoint")
        async def sub_endpoint():
            return {"source": "sub"}

        # Mount WITH leading slash (correct usage)
        main_api.mount("/mount", sub_api)

        registered_paths = [path for _method, path, _handler_id, _handler in main_api._routes]
        assert "/mount/endpoint" in registered_paths, f"Expected '/mount/endpoint' in routes, got: {registered_paths}"

    def test_mount_with_trailing_slash_stripped(self):
        """Mount path with trailing slash should have it stripped."""
        main_api = BoltAPI()
        sub_api = BoltAPI()

        @sub_api.get("/endpoint")
        async def sub_endpoint():
            return {"source": "sub"}

        # Mount with trailing slash
        main_api.mount("/mount/", sub_api)

        registered_paths = [path for _method, path, _handler_id, _handler in main_api._routes]
        assert "/mount/endpoint" in registered_paths, f"Expected '/mount/endpoint' in routes, got: {registered_paths}"
        # Should NOT have double slash
        assert "/mount//endpoint" not in registered_paths, f"Invalid path with double slash found: {registered_paths}"

    def test_mount_various_path_formats(self):
        """Test that various mount path formats are normalized correctly."""
        test_cases = [
            ("mount", "/mount/endpoint"),  # No leading slash
            ("/mount", "/mount/endpoint"),  # With leading slash
            ("mount/", "/mount/endpoint"),  # Trailing slash only
            ("/mount/", "/mount/endpoint"),  # Both slashes
            ("api/v1", "/api/v1/endpoint"),  # Nested path no leading
            ("/api/v1", "/api/v1/endpoint"),  # Nested path with leading
        ]

        for mount_path, expected_route in test_cases:
            main_api = BoltAPI()
            sub_api = BoltAPI()

            @sub_api.get("/endpoint")
            async def sub_endpoint():
                return {"source": "sub"}

            main_api.mount(mount_path, sub_api)

            registered_paths = [path for _method, path, _handler_id, _handler in main_api._routes]
            assert expected_route in registered_paths, (
                f"Mount path '{mount_path}' should result in route '{expected_route}', got: {registered_paths}"
            )

    def test_mounted_routes_are_accessible(self):
        """Test that mounted routes are actually accessible via HTTP."""
        main_api = BoltAPI()
        sub_api = BoltAPI()

        @sub_api.get("/hello")
        async def hello():
            return {"message": "Hello from mounted API"}

        # Mount without leading slash - should still work
        main_api.mount("sub", sub_api)

        with TestClient(main_api) as client:
            response = client.get("/sub/hello")
            assert response.status_code == 200, (
                f"Mounted route should be accessible at /sub/hello, got {response.status_code}"
            )
            assert response.json() == {"message": "Hello from mounted API"}

    def test_mount_keeps_each_route_of_both_apis(self):
        """Each HTTP and WebSocket route of the parent and of the mounted API keeps its handler and its settings."""
        main_api = BoltAPI()
        sub_api = BoltAPI()

        @main_api.get("/first", status_code=201)
        async def first():
            return {"route": "first"}

        @main_api.get("/second", status_code=202)
        async def second():
            return {"route": "second"}

        @sub_api.get("/a", status_code=203)
        async def sub_a():
            return {"route": "a"}

        @sub_api.get("/b", status_code=206)
        async def sub_b():
            return {"route": "b"}

        @sub_api.websocket("/ws/a")
        async def ws_a(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("ws a")

        @sub_api.websocket("/ws/b")
        async def ws_b(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("ws b")

        main_api.mount("/sub", sub_api)

        @main_api.get("/third", status_code=207)
        async def third():
            return {"route": "third"}

        with TestClient(main_api) as client:
            for path, status, route in [
                ("/first", 201, "first"),
                ("/second", 202, "second"),
                ("/sub/a", 203, "a"),
                ("/sub/b", 206, "b"),
                ("/third", 207, "third"),
            ]:
                response = client.get(path)
                assert (response.status_code, response.json()) == (status, {"route": route})

        async def receive_first_text(path: str) -> str:
            async with WebSocketTestClient(main_api, path) as ws:
                return await ws.receive_text()

        assert asyncio.run(receive_first_text("/sub/ws/a")) == "ws a"
        assert asyncio.run(receive_first_text("/sub/ws/b")) == "ws b"

    def test_mounted_websocket_route_keeps_its_auth_and_guards(self):
        """A WebSocket route of a mounted API refuses a connection with no credentials, as before the mount."""
        main_api = BoltAPI()
        sub_api = BoltAPI()

        @sub_api.websocket("/ws", auth=[APIKeyAuthentication(api_keys={"secret"})], guards=[IsAuthenticated()])
        async def protected(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("welcome")

        main_api.mount("/sub", sub_api)

        async def connect(headers: dict[str, str]) -> str:
            async with WebSocketTestClient(main_api, "/sub/ws", headers=headers) as ws:
                return await ws.receive_text()

        with pytest.raises(PermissionError):
            asyncio.run(connect({}))
        assert asyncio.run(connect({"X-API-Key": "secret"})) == "welcome"


class TestOpenAPIDocsWithPrefix:
    """Tests for OpenAPI docs behavior when API has a prefix."""

    def test_docs_accessible_at_absolute_path_with_prefix(self):
        """OpenAPI docs should be at /docs/* regardless of API prefix.

        When BoltAPI(prefix="/api") is used, the docs should still be
        accessible at /docs/openapi.json, NOT at /api/docs/openapi.json.
        """
        api = BoltAPI(
            prefix="/api",
            openapi_config=OpenAPIConfig(
                title="Test API",
                version="1.0.0",
            ),
        )

        @api.get("/items")
        async def get_items():
            return {"items": []}

        api._register_openapi_routes()

        with TestClient(api) as client:
            # Docs SHOULD be at /docs (absolute path)
            response = client.get("/docs/openapi.json")
            assert response.status_code == 200, (
                f"Expected docs at /docs/openapi.json to return 200, got {response.status_code}. "
                "OpenAPI docs should be at absolute path regardless of API prefix."
            )

            # Verify it's valid OpenAPI schema
            schema = response.json()
            assert "openapi" in schema
            assert schema["info"]["title"] == "Test API"

            # API routes should be prefixed
            assert "/api/items" in schema["paths"], (
                f"API routes should include prefix. Expected '/api/items' in paths, got: {list(schema['paths'].keys())}"
            )

    def test_docs_not_at_prefixed_path(self):
        """Docs should NOT be accessible at the prefixed path /api/docs/*.

        This verifies that docs routes bypass the prefix.
        """
        api = BoltAPI(
            prefix="/api",
            openapi_config=OpenAPIConfig(
                title="Test API",
                version="1.0.0",
            ),
        )

        @api.get("/items")
        async def get_items():
            return {"items": []}

        api._register_openapi_routes()

        with TestClient(api) as client:
            # Docs should NOT be at /api/docs (prefixed path)
            response = client.get("/api/docs/openapi.json")
            assert response.status_code == 404, (
                f"Expected /api/docs/openapi.json to return 404, got {response.status_code}. "
                "Docs should NOT be at prefixed path - they should be at absolute /docs/*."
            )

    def test_swagger_ui_at_absolute_path_with_prefix(self):
        """Swagger UI should be at /docs/* regardless of API prefix."""
        api = BoltAPI(
            prefix="/api",
            openapi_config=OpenAPIConfig(
                title="Test API",
                version="1.0.0",
                render_plugins=[SwaggerRenderPlugin()],
            ),
        )

        @api.get("/items")
        async def get_items():
            return {"items": []}

        api._register_openapi_routes()

        with TestClient(api) as client:
            # Swagger UI should be at /docs (absolute path)
            response = client.get("/docs")
            assert response.status_code == 200, (
                f"Expected Swagger UI at /docs to return 200, got {response.status_code}"
            )
            assert "text/html" in response.headers.get("content-type", "")

            # Swagger specific page
            response = client.get("/docs/swagger")
            assert response.status_code == 200, (
                f"Expected Swagger UI at /docs/swagger to return 200, got {response.status_code}"
            )

    def test_api_routes_correctly_prefixed(self):
        """API routes should still be correctly prefixed."""
        api = BoltAPI(
            prefix="/api",
            openapi_config=OpenAPIConfig(
                title="Test API",
                version="1.0.0",
            ),
        )

        @api.get("/items")
        async def get_items():
            return {"items": ["item1", "item2"]}

        api._register_openapi_routes()

        with TestClient(api) as client:
            # API route should be at prefixed path
            response = client.get("/api/items")
            assert response.status_code == 200, f"API route should be at /api/items, got {response.status_code}"
            assert response.json() == {"items": ["item1", "item2"]}

            # API route should NOT be at unprefixed path
            response = client.get("/items")
            assert response.status_code == 404, (
                f"API route should NOT be at /items (unprefixed), got {response.status_code}"
            )

    def test_custom_docs_path_with_prefix(self):
        """Custom docs path should also be at absolute path."""
        api = BoltAPI(
            prefix="/api",
            openapi_config=OpenAPIConfig(
                title="Test API",
                version="1.0.0",
                path="/api-docs",  # Custom docs path
            ),
        )

        @api.get("/items")
        async def get_items():
            return {"items": []}

        api._register_openapi_routes()

        with TestClient(api) as client:
            # Docs should be at custom path (absolute)
            response = client.get("/api-docs/openapi.json")
            assert response.status_code == 200, f"Expected docs at /api-docs/openapi.json, got {response.status_code}"

            # Should NOT be at /api/api-docs (prefixed)
            response = client.get("/api/api-docs/openapi.json")
            assert response.status_code == 404, (
                f"Docs should NOT be at prefixed path /api/api-docs/*, got {response.status_code}"
            )

    def test_openapi_schema_excludes_docs_routes(self):
        """OpenAPI schema should exclude docs routes from the API paths."""
        api = BoltAPI(
            prefix="/api",
            openapi_config=OpenAPIConfig(
                title="Test API",
                version="1.0.0",
            ),
        )

        @api.get("/items")
        async def get_items():
            return {"items": []}

        api._register_openapi_routes()

        with TestClient(api) as client:
            response = client.get("/docs/openapi.json")
            schema = response.json()

            # Docs routes should NOT appear in schema
            paths = list(schema["paths"].keys())
            docs_paths = [p for p in paths if "docs" in p or "openapi" in p]
            assert len(docs_paths) == 0, f"OpenAPI schema should not include docs routes, found: {docs_paths}"

            # API routes should appear
            assert "/api/items" in paths, f"API routes should be in schema: {paths}"


class TestIncludeRouter:
    """Tests for the arguments that include_router() gives to each route."""

    def test_include_router_applies_prefix_and_router_settings(self):
        """The prefix, the route arguments, and the tags and guards of the router reach each route."""
        api = BoltAPI()
        router = Router(prefix="/users", tags=["people"], guards=[IsAuthenticated()])

        @router.post("", status_code=201, summary="Add a user", auth=[APIKeyAuthentication(api_keys={"secret"})])
        async def add_user():
            return {"added": True}

        api.include_router(router, prefix="/api/v1/")
        api._register_openapi_routes()

        with TestClient(api) as client:
            assert client.post("/users").status_code == 404
            assert client.post("/api/v1/users").status_code == 401

            response = client.post("/api/v1/users", headers={"X-API-Key": "secret"})
            assert response.status_code == 201
            assert response.json() == {"added": True}

            operation = client.get("/docs/openapi.json").json()["paths"]["/api/v1/users"]["post"]

        assert operation["tags"] == ["people"]
        assert operation["summary"] == "Add a user"


class TestEmptyRouteRegistration:
    """Tests for empty route ("") registration behavior."""

    def test_empty_route_on_regular_api(self):
        """Empty route "" on regular API should be normalized to "/" and accessible."""
        api = BoltAPI()

        @api.get("")
        async def root():
            return {"message": "root"}

        registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
        # Empty route "" should be normalized to "/"
        assert "/" in registered_paths, f"Empty route should be normalized to '/', got: {registered_paths}"

        with TestClient(api) as client:
            response = client.get("/")
            assert response.status_code == 200, f"Empty route should be accessible at /, got {response.status_code}"
            assert response.json() == {"message": "root"}

    def test_empty_route_with_prefix(self):
        """Empty route "" with prefix should be normalized and accessible at prefix path.

        Empty "" becomes "/" then combined with prefix "/api" becomes "/api/",
        which is then normalized to "/api" (trailing slash stripped).
        """
        api = BoltAPI(prefix="/api")

        @api.get("")
        async def api_root():
            return {"message": "api root"}

        registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
        # Empty "" + prefix "/api" = "/api/" -> normalized to "/api"
        assert "/api" in registered_paths, (
            f"Empty route with prefix should be registered as '/api', got: {registered_paths}"
        )

        with TestClient(api) as client:
            response = client.get("/api")
            assert response.status_code == 200, (
                f"Empty route with prefix should be accessible at /api, got {response.status_code}"
            )
            assert response.json() == {"message": "api root"}

    def test_empty_route_in_mounted_api(self):
        """Empty route "" in mounted API should be accessible at mount path.

        Sub-API has route "" which becomes "/". When mounted at "/sub",
        the combined path "/sub/" is normalized to "/sub".
        """
        main_api = BoltAPI()
        sub_api = BoltAPI()

        @sub_api.get("")
        async def sub_root():
            return {"message": "sub root"}

        main_api.mount("/sub", sub_api)

        registered_paths = [path for _method, path, _handler_id, _handler in main_api._routes]
        # Route "/" mounted at "/sub" = "/sub/" -> normalized to "/sub"
        assert "/sub" in registered_paths, f"Empty route in mounted API should be at '/sub', got: {registered_paths}"

        with TestClient(main_api) as client:
            response = client.get("/sub")
            assert response.status_code == 200, (
                f"Empty route in mounted API should be accessible at /sub, got {response.status_code}"
            )
            assert response.json() == {"message": "sub root"}

    def test_empty_route_in_mounted_api_without_leading_slash(self):
        """Empty route "" in mounted API (mount path without leading slash).

        Mount path "sub" is normalized to "/sub", then combined with "/" gives "/sub/",
        which is normalized to "/sub".
        """
        main_api = BoltAPI()
        sub_api = BoltAPI()

        @sub_api.get("")
        async def sub_root():
            return {"message": "sub root"}

        # Mount without leading slash - should still work
        main_api.mount("sub", sub_api)

        registered_paths = [path for _method, path, _handler_id, _handler in main_api._routes]
        # Mount "sub" normalized to "/sub", + "/" = "/sub/" -> normalized to "/sub"
        assert "/sub" in registered_paths, f"Empty route in mounted API should be at '/sub', got: {registered_paths}"

        with TestClient(main_api) as client:
            response = client.get("/sub")
            assert response.status_code == 200, (
                f"Empty route in mounted API should be accessible at /sub, got {response.status_code}"
            )
            assert response.json() == {"message": "sub root"}

    def test_slash_route_equivalent_to_empty(self):
        """Route "/" should work the same as empty route ""."""
        api = BoltAPI()

        @api.get("/")
        async def root():
            return {"message": "root with slash"}

        with TestClient(api) as client:
            response = client.get("/")
            assert response.status_code == 200, f"Slash route should be accessible at /, got {response.status_code}"
            assert response.json() == {"message": "root with slash"}

    def test_slash_route_with_prefix(self):
        """Route "/" with prefix should be accessible at prefix path.

        Route "/" with prefix "/api" = "/api/" which is normalized to "/api".
        """
        api = BoltAPI(prefix="/api")

        @api.get("/")
        async def api_root():
            return {"message": "api root with slash"}

        registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
        # Route "/" with prefix "/api" = "/api/" -> normalized to "/api"
        assert "/api" in registered_paths, (
            f"Slash route with prefix should be registered as '/api', got: {registered_paths}"
        )

        with TestClient(api) as client:
            response = client.get("/api")
            assert response.status_code == 200, (
                f"Slash route with prefix should be accessible at /api, got {response.status_code}"
            )
            assert response.json() == {"message": "api root with slash"}

    def test_mounted_api_with_prefix_and_empty_route(self):
        """Mounted API with its own prefix and empty route.

        Sub-API has prefix "/v1" and empty route "" which becomes "/v1/".
        When mounted at "/api", combined path is "/api/v1/" which normalizes to "/api/v1".
        """
        main_api = BoltAPI()
        sub_api = BoltAPI(prefix="/v1")

        @sub_api.get("")
        async def versioned_root():
            return {"message": "v1 root"}

        main_api.mount("/api", sub_api)

        with TestClient(main_api) as client:
            response = client.get("/api/v1")
            assert response.status_code == 200, (
                f"Mounted API with prefix and empty route should be at /api/v1, got {response.status_code}"
            )
            assert response.json() == {"message": "v1 root"}


class TestTrailingSlashSetting:
    """Tests for trailing_slash parameter behavior."""

    def test_trailing_slash_strip_default(self):
        """Default mode 'strip' removes trailing slashes."""
        api = BoltAPI()  # default is trailing_slash="strip"

        @api.get("/users/")
        async def users():
            return {"users": []}

        registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
        assert "/users" in registered_paths, (
            f"Default 'strip' mode should remove trailing slash, got: {registered_paths}"
        )

        with TestClient(api) as client:
            response = client.get("/users")
            assert response.status_code == 200

    def test_trailing_slash_strip_explicit(self):
        """Explicit 'strip' mode removes trailing slashes."""
        api = BoltAPI(trailing_slash="strip")

        @api.get("/items/")
        async def items():
            return {"items": []}

        registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
        assert "/items" in registered_paths
        assert "/items/" not in registered_paths

    def test_trailing_slash_append(self):
        """Mode 'append' adds trailing slashes."""
        api = BoltAPI(trailing_slash="append")

        @api.get("/users")
        async def users():
            return {"users": []}

        registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
        assert "/users/" in registered_paths, f"'append' mode should add trailing slash, got: {registered_paths}"
        assert "/users" not in registered_paths

        with TestClient(api) as client:
            response = client.get("/users/")
            assert response.status_code == 200

    def test_trailing_slash_append_already_has_slash(self):
        """Mode 'append' keeps existing trailing slash."""
        api = BoltAPI(trailing_slash="append")

        @api.get("/items/")
        async def items():
            return {"items": []}

        registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
        assert "/items/" in registered_paths

    def test_trailing_slash_keep(self):
        """Mode 'keep' preserves paths as-is."""
        api = BoltAPI(trailing_slash="keep")

        @api.get("/with-slash/")
        async def with_slash():
            return {"slash": True}

        @api.get("/no-slash")
        async def no_slash():
            return {"slash": False}

        registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
        assert "/with-slash/" in registered_paths, f"'keep' should preserve slash, got: {registered_paths}"
        assert "/no-slash" in registered_paths, f"'keep' should preserve no slash, got: {registered_paths}"

    def test_trailing_slash_with_prefix_strip(self):
        """Strip mode with prefix normalizes combined path."""
        api = BoltAPI(prefix="/api", trailing_slash="strip")

        @api.get("/users/")
        async def users():
            return {"users": []}

        registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
        assert "/api/users" in registered_paths, f"Expected '/api/users', got: {registered_paths}"

    def test_trailing_slash_with_prefix_append(self):
        """Append mode with prefix adds trailing slash to combined path."""
        api = BoltAPI(prefix="/api", trailing_slash="append")

        @api.get("/users")
        async def users():
            return {"users": []}

        registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
        assert "/api/users/" in registered_paths, f"Expected '/api/users/', got: {registered_paths}"

    def test_trailing_slash_mount_inherits_parent(self):
        """Mounted routes use parent's trailing_slash setting."""
        main_api = BoltAPI(trailing_slash="append")
        sub_api = BoltAPI(trailing_slash="strip")  # sub-api setting is ignored when mounted

        @sub_api.get("/endpoint")
        async def endpoint():
            return {"data": "test"}

        main_api.mount("/sub", sub_api)

        registered_paths = [path for _method, path, _handler_id, _handler in main_api._routes]
        # Parent has "append" mode, so final path should have trailing slash
        assert "/sub/endpoint/" in registered_paths, (
            f"Mounted route should use parent's 'append' mode, got: {registered_paths}"
        )

    def test_trailing_slash_root_path_unchanged(self):
        """Root path '/' is never modified regardless of setting."""
        for mode in ["strip", "append", "keep"]:
            api = BoltAPI(trailing_slash=mode)

            @api.get("/")
            async def root():
                return {"root": True}

            registered_paths = [path for _method, path, _handler_id, _handler in api._routes]
            assert "/" in registered_paths, f"Root '/' should be unchanged in '{mode}' mode"


class TagMiddleware(Middleware):
    """Add the header ``x-api`` with the name of the API that runs the middleware."""

    name = ""

    async def process_request(self, request):
        response = await self.get_response(request)
        response.headers["x-api"] = self.name
        return response


class ChildTag(TagMiddleware):
    name = "child"


class ParentTag(TagMiddleware):
    name = "parent"


@pytest.mark.parametrize("handler_is_async", [False, True], ids=["sync", "async"])
def test_a_mounted_api_runs_its_own_python_middleware(handler_is_async):
    """The API that registers a route runs its global middleware, also after a mount."""
    child = BoltAPI(middleware=[ChildTag])
    plain_child = BoltAPI()

    if handler_is_async:

        @child.get("/items")
        async def child_items():
            return {"from": "child"}

    else:

        @child.get("/items")
        def child_items():
            return {"from": "child"}

    # The await sends the route through the async dispatch, which picks the middleware.
    @plain_child.get("/items")
    async def plain_items():
        await asyncio.sleep(0)
        return {"from": "plain"}

    parent = BoltAPI()
    parent.mount("/child", child)
    tagged_parent = BoltAPI(middleware=[ParentTag])
    tagged_parent.mount("/plain", plain_child)

    with TestClient(parent) as client:
        child_response = client.get("/child/items")
    with TestClient(tagged_parent) as client:
        plain_response = client.get("/plain/items")

    assert (child_response.json(), child_response.headers.get("x-api")) == ({"from": "child"}, "child")
    assert (plain_response.json(), plain_response.headers.get("x-api")) == ({"from": "plain"}, None)
