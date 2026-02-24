"""
Tests for synchronous class-based views.

This test suite verifies that class-based views work correctly with sync handlers,
including:
- Basic sync APIView functionality
- Parameter extraction and validation
- Guards and authentication
- Response types and serialization
- Inline vs spawn_blocking execution modes
- Error handling in sync views
"""

from __future__ import annotations

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.auth.backends import JWTAuthentication
from django_bolt.auth.guards import IsAuthenticated  # noqa: PLC0415
from django_bolt.exceptions import HTTPException
from django_bolt.params import Depends
from django_bolt.testing import TestClient
from django_bolt.views import APIView

# --- Schema Definitions ---


class CreateUserRequest(msgspec.Struct):
    username: str
    email: str


class ItemSchema(msgspec.Struct):
    name: str
    price: float


class UserSchema(msgspec.Struct):
    name: str
    email: str


class InputSchema(msgspec.Struct):
    name: str
    age: int


# --- Test Fixtures ---


@pytest.fixture
def api():
    """Create a fresh BoltAPI instance for each test."""
    return BoltAPI()


# --- Basic Sync APIView Tests ---


def test_sync_api_view_basic(api):
    """Test basic synchronous APIView with GET handler."""

    @api.view("/hello")
    class HelloView(APIView):
        def get(self, request) -> dict:
            return {"message": "Hello from sync"}

    assert len(api._routes) == 1
    method, path, handler_id, handler = api._routes[0]
    assert method == "GET"
    assert path == "/hello"


def test_sync_api_view_with_client(api):
    """Test sync APIView with TestClient."""

    @api.view("/hello")
    class HelloView(APIView):
        def get(self, request) -> dict:
            return {"message": "Hello from sync"}

    with TestClient(api) as client:
        response = client.get("/hello")
        assert response.status_code == 200
        assert response.json() == {"message": "Hello from sync"}


def test_sync_api_view_multiple_methods(api):
    """Test sync view with multiple HTTP methods."""

    @api.view("/items")
    class ItemView(APIView):
        def get(self, request) -> dict:
            return {"method": "GET", "action": "list"}

        def post(self, request) -> dict:
            return {"method": "POST", "action": "create"}

        def put(self, request) -> dict:
            return {"method": "PUT", "action": "update"}

    with TestClient(api) as client:
        # Test GET
        response = client.get("/items")
        assert response.status_code == 200
        assert response.json()["method"] == "GET"

        # Test POST
        response = client.post("/items")
        assert response.status_code == 200
        assert response.json()["method"] == "POST"

        # Test PUT
        response = client.put("/items")
        assert response.status_code == 200
        assert response.json()["method"] == "PUT"


def test_sync_api_view_path_parameters(api):
    """Test path parameter extraction in sync class-based views."""

    @api.view("/users/{user_id}")
    class UserView(APIView):
        def get(self, request, user_id: int) -> dict:
            return {"user_id": user_id, "type": type(user_id).__name__}

    with TestClient(api) as client:
        response = client.get("/users/123")
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == 123
        assert data["type"] == "int"


def test_sync_api_view_query_parameters(api):
    """Test query parameter extraction in sync class-based views."""

    @api.view("/search")
    class SearchView(APIView):
        def get(self, request, q: str, limit: int = 10) -> dict:
            return {"query": q, "limit": limit}

    with TestClient(api) as client:
        # Test with both params
        response = client.get("/search?q=python&limit=20")
        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "python"
        assert data["limit"] == 20

        # Test with default
        response = client.get("/search?q=django")
        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "django"
        assert data["limit"] == 10


def test_sync_api_view_request_body(api):
    """Test request body parsing in sync class-based views."""

    @api.view("/users")
    class UserCreateView(APIView):
        def post(self, request, data: CreateUserRequest) -> dict:
            return {"username": data.username, "email": data.email, "created": True}

    with TestClient(api) as client:
        response = client.post("/users", json={"username": "john", "email": "john@example.com"})
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "john"
        assert data["email"] == "john@example.com"
        assert data["created"] is True


def test_sync_api_view_invalid_body(api):
    """Test validation error for invalid sync request body."""

    @api.view("/users")
    class UserCreateView(APIView):
        def post(self, request, data: CreateUserRequest) -> dict:
            return {"username": data.username, "email": data.email}

    with TestClient(api) as client:
        # Missing required field
        response = client.post(
            "/users",
            json={"username": "john"},  # Missing email
        )
        assert response.status_code == 422


# --- Sync APIView with Dependencies ---


def test_sync_api_view_dependency_injection(api):
    """Test dependency injection in sync class-based views."""

    async def get_current_user(request) -> dict:
        return {"id": 1, "username": "testuser"}

    @api.view("/profile")
    class ProfileView(APIView):
        def get(self, request, current_user=Depends(get_current_user)) -> dict:
            return {"user_id": current_user["id"], "username": current_user["username"]}

    with TestClient(api) as client:
        response = client.get("/profile")
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == 1
        assert data["username"] == "testuser"


def test_sync_api_view_multiple_dependencies(api):
    """Test multiple dependencies in sync class-based views."""

    def get_user(request) -> dict:
        return {"id": 1, "name": "John"}

    def get_settings(request) -> dict:
        return {"theme": "dark", "language": "en"}

    @api.view("/dashboard")
    class DashboardView(APIView):
        def get(self, request, user=Depends(get_user), settings=Depends(get_settings)) -> dict:
            return {"user": user, "settings": settings}

    with TestClient(api) as client:
        response = client.get("/dashboard")
        assert response.status_code == 200
        data = response.json()
        assert data["user"]["id"] == 1
        assert data["settings"]["theme"] == "dark"


# --- Sync APIView with Guards and Auth ---


def test_sync_api_view_class_level_guards(api):
    """Test class-level guards on sync APIView."""

    @api.view("/protected")
    class ProtectedView(APIView):
        guards = [IsAuthenticated()]

        def get(self, request) -> dict:
            return {"protected": True}

    handler_id = api._routes[0][2]
    middleware_meta = api._handler_middleware.get(handler_id)
    assert middleware_meta is not None
    assert "guards" in middleware_meta


def test_sync_api_view_auth_backend(api):
    """Test class-level authentication on sync APIView."""

    @api.view("/auth-endpoint")
    class AuthView(APIView):
        auth = [JWTAuthentication()]

        def get(self, request) -> dict:
            return {"authenticated": True}

    handler_id = api._routes[0][2]
    middleware_meta = api._handler_middleware.get(handler_id)
    assert middleware_meta is not None
    assert "auth_backends" in middleware_meta


def test_sync_api_view_status_code_override(api):
    """Test status code override in sync APIView."""

    @api.view("/created")
    class CreatedView(APIView):
        status_code = 201

        def post(self, request) -> dict:
            return {"created": True}

    method, path, handler_id, handler = api._routes[0]
    meta = api._handler_meta.get(handler_id)
    assert meta is not None
    assert meta.get("default_status_code") == 201


def test_sync_create_api(api):
    """Test post with sync handler (data validation)."""

    @api.view("/items")
    class ItemCreateView(APIView):
        def post(self, request, data: ItemSchema) -> dict:
            return {"name": data.name, "price": data.price, "created": True}

    with TestClient(api) as client:
        response = client.post("/items", json={"name": "Widget", "price": 9.99})
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Widget"
        assert data["price"] == 9.99


# --- Sync Handler Metadata Tests ---


def test_sync_handler_is_sync_metadata(api):
    """Test that sync handlers are correctly marked as sync."""

    @api.view("/sync-endpoint")
    class SyncView(APIView):
        def get(self, request) -> dict:
            return {"sync": True}

    method, path, handler_id, handler = api._routes[0]
    meta = api._handler_meta.get(handler_id)
    assert meta is not None
    assert meta.get("is_async") is False  # Should be sync


def test_async_handler_is_async_metadata(api):
    """Test that async handlers are correctly marked as async."""

    @api.view("/async-endpoint")
    class AsyncView(APIView):
        async def get(self, request) -> dict:
            return {"async": True}

    method, path, handler_id, handler = api._routes[0]
    meta = api._handler_meta.get(handler_id)
    assert meta is not None
    assert meta.get("is_async") is True  # Should be async


# --- Sync vs Async Parity Tests ---


def test_sync_and_async_same_response(api):
    """Test that sync and async handlers produce same response structure."""

    @api.view("/sync")
    class SyncView(APIView):
        def get(self, request) -> dict:
            return {"value": 42, "type": "sync"}

    @api.view("/async")
    class AsyncView(APIView):
        async def get(self, request) -> dict:
            return {"value": 42, "type": "async"}

    with TestClient(api) as client:
        sync_response = client.get("/sync").json()
        async_response = client.get("/async").json()

        assert sync_response["value"] == async_response["value"]
        assert "value" in sync_response
        assert "value" in async_response


def test_sync_and_async_same_validation(api):
    """Test that sync and async handlers have same validation."""

    @api.view("/sync")
    class SyncView(APIView):
        def post(self, request, data: InputSchema) -> dict:
            return {"name": data.name, "age": data.age}

    @api.view("/async")
    class AsyncView(APIView):
        async def post(self, request, data: InputSchema) -> dict:
            return {"name": data.name, "age": data.age}

    with TestClient(api) as client:
        valid_data = {"name": "John", "age": 30}

        # Test valid data works for both
        sync_response = client.post("/sync", json=valid_data).status_code
        async_response = client.post("/async", json=valid_data).status_code
        assert sync_response == 200
        assert async_response == 200

        # Test invalid data fails for both
        invalid_data = {"name": "John"}  # Missing age
        sync_response = client.post("/sync", json=invalid_data).status_code
        async_response = client.post("/async", json=invalid_data).status_code
        assert sync_response == 422
        assert async_response == 422


def test_sync_and_async_same_parameters(api):
    """Test that sync and async handlers extract same parameters."""

    @api.view("/sync/{id}")
    class SyncView(APIView):
        def get(self, request, id: int, q: str = "default") -> dict:
            return {"id": id, "query": q, "handler": "sync"}

    @api.view("/async/{id}")
    class AsyncView(APIView):
        async def get(self, request, id: int, q: str = "default") -> dict:
            return {"id": id, "query": q, "handler": "async"}

    with TestClient(api) as client:
        sync_data = client.get("/sync/123?q=test").json()
        async_data = client.get("/async/123?q=test").json()

        assert sync_data["id"] == async_data["id"] == 123
        assert sync_data["query"] == async_data["query"] == "test"


# --- Error Handling in Sync Views ---


def test_sync_view_http_exception(api):
    """Test HTTPException handling in sync view."""

    @api.view("/error")
    class ErrorView(APIView):
        def get(self, request) -> dict:
            raise HTTPException(status_code=403, detail="Forbidden")

    with TestClient(api) as client:
        response = client.get("/error")
        assert response.status_code == 403
        assert response.json()["detail"] == "Forbidden"


def test_sync_view_generic_exception(api):
    """Test generic exception handling in sync view."""

    @api.view("/crash")
    class CrashView(APIView):
        def get(self, request) -> dict:
            raise ValueError("Something went wrong")

    with TestClient(api) as client:
        response = client.get("/crash")
        assert response.status_code == 500


def test_sync_view_missing_required_param(api):
    """Test missing required parameter in sync view."""

    @api.view("/search")
    class SearchView(APIView):
        def get(self, request, q: str) -> dict:
            return {"query": q}

    with TestClient(api) as client:
        response = client.get("/search")
        assert response.status_code in (400, 422)


# --- Selective Method Registration ---


def test_sync_view_selective_methods(api):
    """Test registering only specific methods from sync view."""

    @api.view("/items", methods=["GET", "POST"])
    class ItemView(APIView):
        def get(self, request) -> dict:
            return {"method": "GET"}

        def post(self, request) -> dict:
            return {"method": "POST"}

        def delete(self, request) -> dict:
            return {"method": "DELETE"}

    assert len(api._routes) == 2
    methods = {route[0] for route in api._routes}
    assert methods == {"GET", "POST"}
    assert "DELETE" not in methods


# --- Non-Async Handler Validation ---


def test_sync_handler_raises_on_non_async_with_async_decorator():
    """Test that sync handlers still work (not decorated with async)."""
    api = BoltAPI()

    @api.view("/sync")
    class SyncView(APIView):
        def get(self, request) -> dict:
            # This is sync (no async keyword)
            return {"sync": True}

    # Should register successfully
    assert len(api._routes) == 1
    assert api._routes[0][0] == "GET"
