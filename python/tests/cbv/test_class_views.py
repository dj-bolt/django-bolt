"""
Tests for class-based views.

Tests cover:
- Basic APIView functionality
- Parameter extraction and dependency injection
- Guards and authentication
- Return type annotations
"""

from typing import Any

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.auth.backends import JWTAuthentication
from django_bolt.auth.guards import IsAuthenticated  # noqa: PLC0415
from django_bolt.exceptions import HTTPException
from django_bolt.params import Depends
from django_bolt.serializers import Serializer
from django_bolt.views import (
    APIView,
    ViewSet,
)

# --- Test Fixtures ---


@pytest.fixture
def api():
    """Create a fresh BoltAPI instance for each test."""
    return BoltAPI()


def create_request(
    path_params: dict[str, Any] = None,
    query_params: dict[str, Any] = None,
    headers: dict[str, str] = None,
    body: bytes = b"{}",
    auth: dict[str, Any] = None,
) -> dict[str, Any]:
    """Helper to create mock request dictionary."""
    return {
        "params": path_params or {},
        "query": query_params or {},
        "headers": headers or {},
        "cookies": {},
        "body": body,
        "auth": auth or {},
        "method": "GET",
        "path": "/",
    }


def get_route_meta(api, method: str, path: str):
    """Helper to get metadata for a route by method and path."""
    for route_method, route_path, handler_id, _ in api._routes:
        if route_method == method and route_path == path:
            return api._handler_meta.get(handler_id)
    return None


# --- Basic Tests ---


def test_bolt_api_view_basic(api):
    """Test basic APIView with GET handler."""

    @api.view("/hello")
    class HelloView(APIView):
        async def get(self, request) -> dict:
            return {"message": "Hello"}

    # Verify route was registered
    assert len(api._routes) == 1
    method, path, handler_id, handler = api._routes[0]
    assert method == "GET"
    assert path == "/hello"


@pytest.mark.asyncio
async def test_bolt_api_view_dispatch(api):
    """Test that view handlers are actually called."""

    @api.view("/hello")
    class HelloView(APIView):
        async def get(self, request) -> dict:
            return {"message": "Hello, World!"}

    # Get the registered handler
    handler = api._routes[0][3]
    request = create_request()

    # Dispatch and verify result
    result = await handler(request)
    assert result == {"message": "Hello, World!"}


@pytest.mark.asyncio
async def test_bolt_api_view_multiple_methods(api):
    """Test view with multiple HTTP methods."""

    @api.view("/multi")
    class MultiMethodView(APIView):
        async def get(self, request) -> dict:
            return {"method": "GET"}

        async def post(self, request) -> dict:
            return {"method": "POST"}

        async def put(self, request) -> dict:
            return {"method": "PUT"}

    # Verify all methods registered
    assert len(api._routes) == 3
    methods = {route[0] for route in api._routes}
    assert methods == {"GET", "POST", "PUT"}


@pytest.mark.asyncio
async def test_bolt_api_view_path_params(api):
    """Test path parameter extraction in class-based views."""

    @api.view("/users/{user_id}")
    class UserView(APIView):
        async def get(self, request, user_id: int) -> dict:
            return {"user_id": user_id, "type": type(user_id).__name__}

    # Get handler and test with path param
    handler = api._routes[0][3]
    request = create_request(path_params={"user_id": "123"})

    result = await handler(request, user_id=123)  # Rust passes as int
    assert result["user_id"] == 123
    assert result["type"] == "int"


@pytest.mark.asyncio
async def test_bolt_api_view_query_params(api):
    """Test query parameter extraction in class-based views."""

    @api.view("/search")
    class SearchView(APIView):
        async def get(self, request, q: str, limit: int = 10) -> dict:
            return {"query": q, "limit": limit}

    handler = api._routes[0][3]

    # Test with both params
    request = create_request(query_params={"q": "test", "limit": "20"})
    result = await handler(request, q="test", limit=20)
    assert result == {"query": "test", "limit": 20}


@pytest.mark.asyncio
async def test_bolt_api_view_dependency_injection(api):
    """Test dependency injection in class-based views."""

    async def get_current_user(request) -> dict:
        return {"id": 1, "username": "testuser"}

    @api.view("/profile")
    class ProfileView(APIView):
        async def get(self, request, current_user=Depends(get_current_user)) -> dict:
            return {"user": current_user}

    # This test verifies the handler signature is preserved
    handler = api._routes[0][3]

    # Check that handler has correct signature
    import inspect  # noqa: PLC0415

    sig = inspect.signature(handler)
    assert "current_user" in sig.parameters


@pytest.mark.asyncio
async def test_bolt_api_view_request_body(api):
    """Test request body parsing with msgspec.Struct."""

    class CreateUserRequest(msgspec.Struct):
        username: str
        email: str

    @api.view("/users")
    class UserCreateView(APIView):
        async def post(self, request, data: CreateUserRequest) -> dict:
            return {"username": data.username, "email": data.email}

    handler = api._routes[0][3]

    # Verify handler signature includes data parameter
    import inspect  # noqa: PLC0415

    sig = inspect.signature(handler)
    assert "data" in sig.parameters


@pytest.mark.asyncio
async def test_bolt_api_view_return_annotation(api):
    """Test that return annotations are preserved."""

    class ResponseSchema(msgspec.Struct):
        message: str
        count: int

    @api.view("/annotated")
    class AnnotatedView(APIView):
        async def get(self, request) -> ResponseSchema:
            return ResponseSchema(message="test", count=42)

    # Check that handler signature includes return annotation
    handler = api._routes[0][3]
    import inspect  # noqa: PLC0415

    sig = inspect.signature(handler)
    assert sig.return_annotation == ResponseSchema


# --- Guards and Authentication Tests ---


def test_bolt_api_view_class_level_guards(api):
    """Test class-level guards are applied."""

    @api.view("/protected")
    class ProtectedView(APIView):
        guards = [IsAuthenticated()]

        async def get(self, request) -> dict:
            return {"protected": True}

    # Verify middleware metadata includes guards
    handler_id = api._routes[0][2]
    middleware_meta = api._handler_middleware.get(handler_id)
    assert middleware_meta is not None
    assert "guards" in middleware_meta
    assert len(middleware_meta["guards"]) == 1


def test_bolt_api_view_route_level_guard_override(api):
    """Test route-level guards override class-level guards."""
    from django_bolt.auth.guards import IsAdminUser  # noqa: PLC0415

    # Override with route-level guards
    @api.view("/admin", guards=[IsAdminUser()])
    class ViewWithClassGuards(APIView):
        guards = [IsAuthenticated()]

        async def get(self, request) -> dict:
            return {"data": "test"}

    # Verify route-level guards were used
    handler_id = api._routes[0][2]
    middleware_meta = api._handler_middleware.get(handler_id)
    assert middleware_meta is not None
    assert len(middleware_meta["guards"]) == 1
    # Should be is_superuser (IsAdminUser's guard_name), not is_authenticated
    assert middleware_meta["guards"][0]["type"] == "is_superuser"


def test_bolt_api_view_class_level_auth(api):
    """Test class-level authentication backends."""

    @api.view("/auth")
    class AuthView(APIView):
        auth = [JWTAuthentication()]

        async def get(self, request) -> dict:
            return {"authenticated": True}

    # Verify middleware metadata includes auth backends
    handler_id = api._routes[0][2]
    middleware_meta = api._handler_middleware.get(handler_id)
    assert middleware_meta is not None
    assert "auth_backends" in middleware_meta


def test_bolt_api_view_status_code_override(api):
    """Test class-level and route-level status code overrides."""

    @api.view("/items")
    class CreatedView(APIView):
        status_code = 201

        async def post(self, request) -> dict:
            return {"created": True}

    # Verify status code in handler metadata
    method, path, handler_id, handler = api._routes[0]
    meta = api._handler_meta.get(handler_id)
    assert meta is not None
    assert meta.get("default_status_code") == 201


def test_bolt_api_view_get_allowed_methods():
    """Test APIView correctly identifies implemented methods."""

    class UserAPIView(APIView):
        async def get(self, request):
            return {"method": "list"}

        async def post(self, request):
            return {"method": "create"}

    allowed = UserAPIView.get_allowed_methods()
    assert "GET" in allowed
    assert "POST" in allowed
    assert "DELETE" not in allowed


# --- Edge Cases and Validation ---


def test_bolt_api_view_non_subclass_raises():
    """Test that non-APIView classes raise TypeError."""

    api = BoltAPI()

    with pytest.raises(TypeError) as exc_info:

        @api.view("/bad")
        class NotAView:
            async def get(self, request):
                return {}

    assert "must inherit from APIView" in str(exc_info.value)


def test_bolt_api_view_no_methods_raises():
    """Test that view with no methods raises ValueError."""

    api = BoltAPI()

    with pytest.raises(ValueError) as exc_info:

        @api.view("/empty")
        class EmptyView(APIView):
            http_method_names = []

    assert "does not implement any HTTP methods" in str(exc_info.value)


def test_bolt_api_view_selective_method_registration(api):
    """Test registering only specific methods from a view."""

    # Only register GET and POST
    @api.view("/items", methods=["GET", "POST"])
    class MultiMethodView(APIView):
        async def get(self, request) -> dict:
            return {"method": "GET"}

        async def post(self, request) -> dict:
            return {"method": "POST"}

        async def delete(self, request) -> dict:
            return {"method": "DELETE"}

    # Verify only 2 methods registered
    assert len(api._routes) == 2
    methods = {route[0] for route in api._routes}
    assert methods == {"GET", "POST"}
    assert "DELETE" not in methods


def test_bolt_api_view_unimplemented_method_raises(api):
    """Test requesting unimplemented method raises ValueError."""

    with pytest.raises(ValueError) as exc_info:

        @api.view("/items", methods=["POST"])
        class GetOnlyView(APIView):
            async def get(self, request) -> dict:
                return {"method": "GET"}

    assert "does not implement method 'post'" in str(exc_info.value)


# --- Integration Tests ---


def test_bolt_api_view_method_names_customization():
    """Test customizing http_method_names."""

    api = BoltAPI()

    @api.view("/limited")
    class GetOnlyView(APIView):
        http_method_names = ["get"]

        async def get(self, request) -> dict:
            return {"method": "GET"}

        async def post(self, request) -> dict:
            return {"method": "POST"}

    # Only GET should be registered (POST not in http_method_names)
    assert len(api._routes) == 1
    assert api._routes[0][0] == "GET"


def test_viewset_tags_registration(api):
    """Test tags passed to api.viewset() are correctly registered."""

    class EmptySerializer(Serializer):
        pass

    @api.viewset("/items", tags=["Items", "Public"])
    class TaggedItemViewSet(ViewSet):
        serializer_class = EmptySerializer

        async def list(self, request):
            return []

        async def create(self, request, data: dict):
            return {}

    list_meta = get_route_meta(api, "GET", "/items")
    assert list_meta is not None
    assert list_meta.get("openapi_tags") == ["Items", "Public"]

    create_meta = get_route_meta(api, "POST", "/items")
    assert create_meta is not None
    assert create_meta["openapi_tags"] == ["Items", "Public"]


def test_view_tags_registration(api):
    """Test tags passed to api.view() are correctly registered."""

    @api.view("/products", tags=["Products", "Catalog"])
    class TaggedProductView(APIView):
        async def get(self, request) -> dict:
            return {"products": []}

        async def post(self, request) -> dict:
            return {"created": True}

    get_meta = get_route_meta(api, "GET", "/products")
    assert get_meta is not None
    assert get_meta.get("openapi_tags") == ["Products", "Catalog"]

    post_meta = get_route_meta(api, "POST", "/products")
    assert post_meta is not None
    assert post_meta["openapi_tags"] == ["Products", "Catalog"]
