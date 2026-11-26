"""
Tests for API prefix and mount path behavior.

These tests verify:
1. Mount paths are normalized to include leading slash
2. OpenAPI docs remain at absolute paths regardless of API prefix
"""

import asyncio
from typing import Annotated

import pytest

from django_bolt import BoltAPI, Depends, Router, WebSocket
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated
from django_bolt.middleware import Middleware
from django_bolt.openapi import OpenAPIConfig, SwaggerRenderPlugin
from django_bolt.params import Query
from django_bolt.testing import TestClient, WebSocketTestClient
from django_bolt.views import APIView, ViewSet


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

    @pytest.mark.parametrize("mount_path", ["/", "//", ""])
    def test_mount_at_the_root_keeps_the_route_paths(self, mount_path):
        """A mount at the root serves each route of the mounted API at its own path."""
        main_api = BoltAPI()
        sub_api = BoltAPI()

        @sub_api.get("/hello")
        async def hello():
            return {"message": "hello"}

        @sub_api.get("/")
        async def root():
            return {"message": "root"}

        main_api.mount(mount_path, sub_api)

        with TestClient(main_api) as client:
            response = client.get("/hello")
            assert response.status_code == 200
            assert response.json() == {"message": "hello"}
            response = client.get("/")
            assert response.status_code == 200
            assert response.json() == {"message": "root"}


def test_an_api_cannot_mount_itself():
    """mount() copies the routes of the mounted API. For the API itself, the copy never ends."""
    api = BoltAPI()

    @api.get("/a")
    async def handler():
        return {"ok": True}

    with pytest.raises(ValueError, match="An API cannot mount itself"):
        api.mount("/s", api)
    assert [path for _method, path, _id, _handler in api._routes] == ["/a"]


def test_an_api_cannot_mount_an_api_that_has_its_routes():
    first = BoltAPI()
    second = BoltAPI()

    @first.get("/a")
    async def handler():
        return {"ok": True}

    second.mount("/first", first)
    with pytest.raises(ValueError, match="mount cycle"):
        first.mount("/second", second)


def test_a_mounted_handler_cannot_take_a_mount_path_parameter_as_an_argument():
    """The mounted route was compiled without the mount path, so the argument reads the query."""
    api = BoltAPI()
    child = BoltAPI()

    @child.get("/c")
    async def handler(x: int):
        return {"x": x}

    with pytest.raises(ValueError, match=r"GET /c takes the parameter 'x' of the mount path '/t/\{x\}'"):
        api.mount("/t/{x}", child)
    assert api._routes == []


def _x_sync(x: int) -> int:
    return x


async def _x_async(x: int) -> int:
    return x


def _x_nested(value: Annotated[int, Depends(_x_sync)]) -> int:
    return value


@pytest.mark.parametrize("dependency", [_x_sync, _x_async, _x_nested], ids=["sync", "async", "nested"])
@pytest.mark.parametrize("handler_is_async", [True, False], ids=["async-handler", "sync-handler"])
def test_a_mounted_dependency_cannot_take_a_mount_path_parameter_as_an_argument(dependency, handler_is_async):
    """The dependency was compiled for the route without the mount path, so the argument reads the query."""
    api = BoltAPI()
    child = BoltAPI()

    if handler_is_async:

        @child.get("/c")
        async def handler(value: Annotated[int, Depends(dependency)]):
            return {"x": value}

    else:

        @child.get("/c")
        def handler(value: Annotated[int, Depends(dependency)]):
            return {"x": value}

    with pytest.raises(
        ValueError, match=r"A dependency of the handler of GET /c takes the parameter 'x' of the mount path '/t/\{x\}'"
    ):
        api.mount("/t/{x}", child)
    assert api._routes == []


def test_a_mounted_dependency_reads_a_mount_path_parameter_from_the_request():
    api = BoltAPI()
    child = BoltAPI()

    def from_request(request) -> str:
        return request.params["x"]

    def from_the_query(x: Annotated[int, Query()] = 0) -> int:
        return x

    @child.get("/c")
    async def handler(
        path_value: Annotated[str, Depends(from_request)], query_value: Annotated[int, Depends(from_the_query)]
    ):
        return {"path": path_value, "query": query_value}

    api.mount("/t/{x:int}", child)
    with TestClient(api) as client:
        assert client.get("/t/5/c?x=9").json() == {"path": "5", "query": 9}


def test_a_mounted_handler_reads_a_mount_path_parameter_from_the_request():
    api = BoltAPI()
    child = BoltAPI()

    @child.get("/c")
    async def from_request(request):
        return {"x": request.params["x"]}

    @child.get("/items/{item_id}")
    async def own_parameter(item_id: int, x: Annotated[int, Query()] = 0):
        return {"item_id": item_id, "x": x}

    api.mount("/t/{x:int}", child)
    with TestClient(api) as client:
        assert client.get("/t/5/c").json() == {"x": "5"}
        assert client.get("/t/5/items/7?x=9").json() == {"item_id": 7, "x": 9}


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

    def test_prefix_with_trailing_slash(self):
        """The trailing slash of the prefix does not give a double slash in the route path."""
        api = BoltAPI(prefix="/api/v1/")

        @api.get("/users")
        async def list_users():
            return {"users": []}

        with TestClient(api) as client:
            assert client.get("/api/v1/users").status_code == 200

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


@pytest.mark.parametrize(
    ("route", "url", "expected"),
    [
        ("/ü", "/%C3%BC", {"route": "/ü"}),
        ("/a b", "/a%20b", {"route": "/a b"}),
        ("/ü/{x}", "/%C3%BC/3", {"route": "/ü/{x}", "x": 3}),
        ("/a b/{x}/ü", "/a%20b/3/%C3%BC", {"route": "/a b/{x}/ü", "x": 3}),
        # A path that has the escapes already keeps them.
        ("/%C3%A9", "/%C3%A9", {"route": "/%C3%A9"}),
    ],
)
def test_a_route_path_with_a_character_that_a_url_escapes_matches_the_request(route, url, expected):
    """A client sends such a character as a percent escape, and the route answers."""
    api = BoltAPI()

    if "{x}" in route:

        @api.get(route)
        async def handler(x: int):
            return {"route": route, "x": x}

    else:

        @api.get(route)
        async def handler():
            return {"route": route}

    with TestClient(api) as client:
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.json() == expected


@pytest.mark.parametrize(
    ("route", "url", "expected"),
    [
        # RFC 3986 section 6.2.2.1: the letter case of an escape has no meaning.
        ("/ü", "/%c3%bc", {"route": "/ü"}),
        ("/a b/{x}", "/a%20b/a%2fb", {"route": "/a b/{x}", "x": "a/b"}),
        ("/%c3%a9", "/%C3%A9", {"route": "/%c3%a9"}),
        # RFC 3986 section 6.2.2.2: an escape of an unreserved character is that character.
        ("/a", "/%61", {"route": "/a"}),
        ("/%61", "/a", {"route": "/%61"}),
        ("/a-b/{x}", "/a%2Db/%61", {"route": "/a-b/{x}", "x": "a"}),
        # Django decodes each escape before it matches. So does Bolt, but not `%2F` and `%25`.
        ("/a:b,c", "/a%3Ab%2cc", {"route": "/a:b,c"}),
        ("/files/{x}", "/files/a%2Fb%2541", {"route": "/files/{x}", "x": "a/b%41"}),
    ],
)
def test_an_escape_in_another_form_matches_the_same_route(route, url, expected):
    """A client can write an escape in lower case, or escape a character that needs no escape."""
    api = BoltAPI()

    if "{x}" in route:

        @api.get(route)
        async def handler(x: str):
            return {"route": route, "x": x}

    else:

        @api.get(route)
        async def handler():
            return {"route": route}

    with TestClient(api) as client:
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.json() == expected


def test_an_escaped_slash_does_not_split_a_segment():
    """`%2F` is data of one segment. It does not match the `/` between two segments."""
    api = BoltAPI()

    @api.get("/a/b")
    async def handler():
        return {"ok": True}

    with TestClient(api) as client:
        assert client.get("/a/b").status_code == 200
        assert client.get("/a%2Fb").status_code == 404
        assert client.get("/a%2fb").status_code == 404


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

    def test_mount_on_api_with_prefix(self):
        """The prefix of the parent API applies to a mounted route, as to each other route of the parent."""
        main_api = BoltAPI(prefix="/api")
        sub_api = BoltAPI()

        @main_api.get("/health")
        async def health():
            return {"ok": True}

        @sub_api.get("/users")
        async def list_users():
            return {"users": []}

        main_api.mount("/v1", sub_api)

        with TestClient(main_api) as client:
            assert client.get("/api/health").status_code == 200
            response = client.get("/api/v1/users")
            assert response.status_code == 200
            assert response.json() == {"users": []}
            assert client.get("/v1/users").status_code == 404


@pytest.mark.parametrize("path", ["/d", "/d/{x}"])
def test_a_second_handler_for_the_method_and_path_of_a_route_stops_startup(path):
    """A route does not replace an earlier route with no sign."""
    api = BoltAPI()

    @api.get(path)
    async def first():
        return {"handler": "first"}

    @api.get(path)
    async def second():
        return {"handler": "second"}

    with pytest.raises(ValueError, match="Failed to register route"):
        TestClient(api)


def test_two_mounted_apis_with_the_same_route_stop_startup():
    api = BoltAPI()
    for name in ("first", "second"):
        child = BoltAPI()

        @child.get("/d")
        async def handler(name: str = name):
            return {"handler": name}

        api.mount("/m", child)

    with pytest.raises(ValueError, match="GET /m/d is already registered"):
        TestClient(api)


def test_the_same_path_with_another_method_is_another_route():
    api = BoltAPI()

    @api.get("/d")
    async def read():
        return {"method": "GET"}

    @api.post("/d")
    async def write():
        return {"method": "POST"}

    with TestClient(api) as client:
        assert client.get("/d").json() == {"method": "GET"}
        assert client.post("/d").json() == {"method": "POST"}


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

    @pytest.mark.parametrize("value", ["bogus", "Strip", "", None, True])
    def test_trailing_slash_refuses_an_unknown_mode(self, value):
        """An unknown mode behaved as "keep", with no error."""
        with pytest.raises(ValueError, match='trailing_slash must be "strip", "append" or "keep"'):
            BoltAPI(trailing_slash=value)

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


@pytest.mark.parametrize(
    "order",
    [("parent",), ("child",), ("parent", "child"), ("child", "parent"), ("nest", "parent", "child")],
    ids=lambda order: "+".join(order),
)
def test_a_router_with_a_parent_registers_one_time_with_the_prefix_of_its_parent(order):
    """The order of the include calls does not change the routes of the child."""
    parent = Router(prefix="/v1")
    child = Router(prefix="/users", parent=parent)

    @parent.get("/status")
    async def status():
        return {"route": "status"}

    @child.get("/me")
    async def me():
        return {"route": "me"}

    api = BoltAPI()
    for name in order:
        if name == "nest":
            # The child is already a child of this parent.
            parent.include_router(child)
        else:
            api.include_router(parent if name == "parent" else child)

    paths = sorted(path for _method, path, _id, handler in api._routes if handler in (status, me))
    assert paths == (["/v1/users/me"] if order == ("child",) else ["/v1/status", "/v1/users/me"])
    with TestClient(api) as client:
        assert client.get("/v1/users/me").json() == {"route": "me"}
        assert client.get("/users/me").status_code == 404


def test_a_router_included_with_two_prefixes_registers_two_times():
    parent = Router(prefix="/v1")
    child = Router(prefix="/users", parent=parent)

    @child.get("/me")
    async def me():
        return {"route": "me"}

    api = BoltAPI()
    api.include_router(parent, prefix="/a")
    api.include_router(parent, prefix="/b")
    api.include_router(child, prefix="/b")

    with TestClient(api) as client:
        assert client.get("/a/v1/users/me").json() == {"route": "me"}
        assert client.get("/b/v1/users/me").json() == {"route": "me"}
    assert sum(handler is me for *_route, handler in api._routes) == 2


def _api_with_route_and_prefix_without_slash():
    api = BoltAPI(prefix="api")

    @api.get("x")
    async def handler():
        return {"ok": True}

    return api


def _api_with_router_without_slash():
    api = BoltAPI()
    router = Router(prefix="users")

    @router.get("x")
    async def handler():
        return {"ok": True}

    @router.get("")
    async def root():
        return {"ok": True}

    api.include_router(router, prefix="v1")
    return api


def _api_with_nested_router_without_slash():
    api = BoltAPI()
    parent = Router(prefix="v1")
    child = Router(prefix="users")
    parent.include_router(child, prefix="admin")

    @child.get("x")
    async def handler():
        return {"ok": True}

    api.include_router(parent)
    return api


def _api_with_view_and_viewset_without_slash():
    api = BoltAPI(prefix="api")

    @api.view("view")
    class View(APIView):
        async def get(self, request):
            return {"ok": True}

    @api.viewset("items")
    class Items(ViewSet):
        async def list(self, request):
            return {"ok": True}

        async def retrieve(self, request, pk: int):
            return {"ok": True}

    return api


@pytest.mark.parametrize(
    ("make_api", "urls"),
    [
        (_api_with_route_and_prefix_without_slash, ["/api/x"]),
        (_api_with_router_without_slash, ["/v1/users/x", "/v1/users"]),
        (_api_with_nested_router_without_slash, ["/v1/admin/users/x"]),
        (_api_with_view_and_viewset_without_slash, ["/api/view", "/api/items", "/api/items/1"]),
    ],
)
def test_a_path_or_prefix_with_no_leading_slash_gets_one(make_api, urls):
    """Each path and prefix follows the rule of the mount path: Bolt adds the leading slash."""
    api = make_api()
    with TestClient(api) as client:
        for url in urls:
            response = client.get(url)
            assert response.status_code == 200, (url, response.text)


@pytest.mark.asyncio
async def test_a_websocket_path_with_no_leading_slash_gets_one():
    api = BoltAPI(prefix="api")

    @api.websocket("ws")
    async def handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")
        await websocket.close()

    async with WebSocketTestClient(api, "/api/ws") as ws:
        assert await ws.receive_text() == "connected"


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


def test_include_router_prefix_applies_to_routes_added_before_and_after():
    """The prefix of Router.include_router applies to each route of the child, whenever it was added."""
    parent = Router(prefix="/v1")
    child = Router(prefix="/users")

    @child.get("/early")
    async def early():
        return {"route": "early"}

    parent.include_router(child, prefix="/extra")

    @child.get("/late")
    async def late():
        return {"route": "late"}

    api = BoltAPI()
    api.include_router(parent)

    with TestClient(api) as client:
        assert client.get("/v1/extra/users/early").json() == {"route": "early"}
        assert client.get("/v1/extra/users/late").json() == {"route": "late"}
