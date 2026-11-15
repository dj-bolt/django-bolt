"""
Integration tests for parameter validation with real-world use cases.

Tests the complete flow from route registration to request handling.
"""

import re
from dataclasses import dataclass
from datetime import date

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.params import Cookie, File, Form, Header, Path, Query
from django_bolt.testing import TestClient


class User(msgspec.Struct):
    id: int
    name: str
    email: str


class UserCreate(msgspec.Struct):
    name: str
    email: str
    password: str


class UserUpdate(msgspec.Struct):
    name: str | None = None
    email: str | None = None


class SearchFilters(msgspec.Struct):
    query: str
    tags: list[str] = []
    min_score: float = 0.0


# ============================================================================
# Use Case 1: CRUD Operations with Proper HTTP Methods
# ============================================================================


def test_crud_operations_validation():
    """Test that CRUD operations use correct HTTP methods and parameters."""
    api = BoltAPI()

    # ✓ List (GET with query params)
    @api.get("/users")
    async def list_users(page: int = 1, limit: int = 10, search: str = ""):
        return {"users": [], "page": page, "limit": limit}

    # ✓ Get single (GET with path param)
    @api.get("/users/{user_id}")
    async def get_user(user_id: int):
        return {"id": user_id, "name": "John"}

    # ✓ Create (POST with body)
    @api.post("/users")
    async def create_user(user: UserCreate):
        return {"id": 1, "name": user.name}

    # ✓ Update (PUT with path + body)
    @api.put("/users/{user_id}")
    async def update_user(user_id: int, user: UserUpdate):
        return {"id": user_id, "updated": True}

    # ✓ Partial update (PATCH with path + body)
    @api.patch("/users/{user_id}")
    async def patch_user(user_id: int, user: UserUpdate):
        return {"id": user_id, "patched": True}

    # ✓ Delete (DELETE with path param, no body)
    @api.delete("/users/{user_id}")
    async def delete_user(user_id: int):
        return {"deleted": True}

    assert len(api._routes) == 6

    # Verify parameter sources
    for method, path, handler_id, _fn in api._routes:
        meta = api._handler_meta[handler_id]

        if method == "GET" and path == "/users":
            # Query params should be inferred
            for field in meta["fields"]:
                assert field.source == "query"

        elif method == "GET" and "user_id" in path:
            # Path param should be detected
            user_id_field = next(f for f in meta["fields"] if f.name == "user_id")
            assert user_id_field.source == "path"

        elif method in ("POST", "PUT", "PATCH"):
            # Should have body params
            body_fields = [f for f in meta["fields"] if f.source == "body"]
            if "POST" in method or "PUT" in method or "PATCH" in method:
                # At least one body field expected
                assert len(body_fields) >= 1 or any(f.source == "path" for f in meta["fields"])


# ============================================================================
# Use Case 2: Search/Filter Endpoints
# ============================================================================


def test_search_endpoints_with_complex_filters():
    """Test search endpoints with multiple query parameters."""
    api = BoltAPI()

    # ✓ Simple search with auto-inference
    @api.get("/search")
    async def simple_search(q: str, limit: int = 10):
        return {"results": [], "query": q}

    # ✓ Advanced search with explicit markers
    @api.get("/advanced-search")
    async def advanced_search(
        query: str = Query(min_length=3),
        category: str = Query(default="all"),
        min_price: float = Query(ge=0, default=0),
        max_price: float = Query(le=10000, default=10000),
        page: int = Query(ge=1, default=1),
    ):
        return {"results": []}

    # ✓ Search with struct body (POST for complex filters)
    @api.post("/search/advanced")
    async def complex_search(filters: SearchFilters):
        return {"results": [], "filters": filters}

    assert len(api._routes) == 3


# ============================================================================
# Use Case 3: Authentication/Authorization Headers
# ============================================================================


def test_authentication_headers():
    """Test endpoints with authentication headers."""
    api = BoltAPI()

    @api.get("/protected")
    async def protected_endpoint(
        authorization: str = Header(alias="Authorization"),
        user_agent: str = Header(alias="User-Agent", default="unknown"),
    ):
        return {"authenticated": True}

    @api.get("/api-key")
    async def api_key_endpoint(api_key: str = Header(alias="X-API-Key")):
        return {"valid": True}

    assert len(api._routes) == 2

    # Verify header sources
    for _method, _path, handler_id, _fn in api._routes:
        meta = api._handler_meta[handler_id]
        for field in meta["fields"]:
            assert field.source == "header"


# ============================================================================
# Use Case 4: File Upload Endpoints
# ============================================================================


def test_file_upload_endpoints():
    """Test file upload with form data."""
    api = BoltAPI()

    @api.post("/upload")
    async def upload_file(file: bytes = File(), description: str = Form(default="")):
        return {"uploaded": True, "size": len(file)}

    @api.post("/upload/multiple")
    async def upload_multiple(files: list[bytes] = File(), category: str = Form()):
        return {"uploaded": len(files)}

    assert len(api._routes) == 2


# ============================================================================
# Use Case 5: Mixed Parameter Sources
# ============================================================================


def test_mixed_parameter_sources():
    """Test endpoints with parameters from multiple sources."""
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def get_item_with_options(
        item_id: int,  # Path
        include_details: bool = False,  # Query (inferred)
        user_agent: str = Header(alias="User-Agent", default="unknown"),  # Header
        session_id: str = Cookie(default=""),  # Cookie
    ):
        return {"item_id": item_id, "details": include_details}

    method, path, handler_id, handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    sources = {f.name: f.source for f in meta["fields"]}
    assert sources["item_id"] == "path"
    assert sources["include_details"] == "query"
    assert sources["user_agent"] == "header"
    assert sources["session_id"] == "cookie"


@dataclass
class OrderUpdate:
    quantity: int


def test_parameters_with_no_marker_get_their_source_from_the_type():
    """With no marker: ``req`` is the request, a dataclass is the body, each other type is a query value."""
    api = BoltAPI()

    @api.post("/orders/{order_id}")
    async def update_order(
        req,
        order_id: int,
        order: OrderUpdate,
        tags: list[int],
        ship_on: date,
        note=None,
    ):
        return {
            "path": req.path,
            "order_id": order_id,
            "quantity": order.quantity,
            "tags": tags,
            "ship_on": ship_on.isoformat(),
            "note": note,
        }

    with TestClient(api) as client:
        response = client.post("/orders/7?tags=1&tags=2&ship_on=2024-01-02&note=fragile", json={"quantity": 3})

    assert response.status_code == 200
    assert response.json() == {
        "path": "/orders/7",
        "order_id": 7,
        "quantity": 3,
        "tags": [1, 2],
        "ship_on": "2024-01-02",
        "note": "fragile",
    }


# ============================================================================
# Use Case 6: Validation Error Cases
# ============================================================================


def test_invalid_get_with_body():
    """Test that GET with body parameter is rejected."""
    api = BoltAPI()

    with pytest.raises(TypeError) as exc_info:

        @api.get("/invalid")
        async def invalid_get(data: UserCreate):
            return {"error": "should not register"}

    assert "GET /invalid cannot have body parameters" in str(exc_info.value)
    assert "data" in str(exc_info.value)


def test_invalid_delete_with_body():
    """Test that DELETE with body parameter is rejected."""
    api = BoltAPI()

    with pytest.raises(TypeError) as exc_info:

        @api.delete("/items/{item_id}")
        async def invalid_delete(item_id: int, data: UserCreate):
            return {"deleted": True}

    assert "DELETE" in str(exc_info.value)
    assert "cannot have body parameters" in str(exc_info.value)


# ============================================================================
# Use Case 7: Nested Routes with Path Parameters
# ============================================================================


def test_nested_resources_with_path_params():
    """Test nested resource routes with multiple path parameters."""
    api = BoltAPI()

    @api.get("/users/{user_id}/posts/{post_id}")
    async def get_user_post(user_id: int, post_id: int):
        return {"user_id": user_id, "post_id": post_id}

    @api.get("/organizations/{org_id}/teams/{team_id}/members/{member_id}")
    async def get_team_member(org_id: int, team_id: int, member_id: int):
        return {"org": org_id, "team": team_id, "member": member_id}

    # Verify all path params detected
    for _method, path, handler_id, _fn in api._routes:
        meta = api._handler_meta[handler_id]
        path_fields = [f for f in meta["fields"] if f.source == "path"]

        # Count expected path params
        expected_params = len(re.findall(r"\{(\w+)\}", path))
        assert len(path_fields) == expected_params


# ============================================================================
# Use Case 8: Optional vs Required Parameters
# ============================================================================


def test_optional_and_required_parameters():
    """Test proper handling of optional and required parameters."""
    api = BoltAPI()

    @api.get("/items")
    async def list_items(
        category: str,  # Required (no default)
        min_price: float = 0,  # Optional (has default)
        max_price: float | None = None,  # Optional (nullable)
        tags: list[str] = None,  # Optional (default empty list)
    ):
        if tags is None:
            tags = []
        return {"items": []}

    method, path, handler_id, handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    category_field = next(f for f in meta["fields"] if f.name == "category")
    min_price_field = next(f for f in meta["fields"] if f.name == "min_price")
    max_price_field = next(f for f in meta["fields"] if f.name == "max_price")

    # Check field definitions
    assert category_field.is_required
    assert not min_price_field.is_required
    assert not max_price_field.is_required


# ============================================================================
# Use Case 9: Explicit Path() Marker (Edge Case)
# ============================================================================


def test_explicit_path_marker():
    """Test explicit Path() marker (rarely needed but supported)."""
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def get_item(item_id: int = Path()):
        return {"id": item_id}

    method, path, handler_id, handler = api._routes[0]
    meta = api._handler_meta[handler_id]
    item_id_field = next(f for f in meta["fields"] if f.name == "item_id")
    assert item_id_field.source == "path"


# ============================================================================
# Use Case 10: Complex Real-World API
# ============================================================================


def test_real_world_api_structure():
    """Test a realistic API with multiple endpoint types."""
    api = BoltAPI(prefix="/api/v1")

    # Auth endpoints
    @api.post("/auth/login")
    async def login(email: str = Form(), password: str = Form()):
        return {"token": "abc123"}

    @api.post("/auth/logout")
    async def logout(authorization: str = Header(alias="Authorization")):
        return {"logged_out": True}

    # User management
    @api.get("/users/{user_id}")
    async def get_user(user_id: int, include_posts: bool = False):
        return {"id": user_id}

    @api.put("/users/{user_id}")
    async def update_user(user_id: int, user: UserUpdate):
        return {"updated": True}

    # Search
    @api.get("/search")
    async def search(q: str, type: str = "all", page: int = 1):
        return {"results": []}

    # Analytics (complex filters via POST)
    @api.post("/analytics/report")
    async def generate_report(filters: SearchFilters):
        return {"report_id": "r123"}

    assert len(api._routes) == 6

    # Verify no GET endpoints have body params
    for method, path, handler_id, _fn in api._routes:
        if method == "GET":
            meta = api._handler_meta[handler_id]
            body_fields = [f for f in meta["fields"] if f.source == "body"]
            assert len(body_fields) == 0, f"GET {path} should not have body params"


# ============================================================================
# Use Case 11: Error Messages Quality
# ============================================================================


def test_error_message_includes_all_solutions():
    """Verify error messages provide all necessary information."""
    api = BoltAPI()

    with pytest.raises(TypeError) as exc_info:

        @api.get("/bad-endpoint")
        async def bad_handler(complex_data: User):
            return {"data": complex_data}

    error_msg = str(exc_info.value)

    # Should include handler name
    assert "bad_handler" in error_msg

    # Should include method and path
    assert "GET" in error_msg
    assert "/bad-endpoint" in error_msg

    # Should list problematic parameters
    assert "complex_data" in error_msg

    # Should provide solutions
    assert "Solutions:" in error_msg
    assert "POST/PUT/PATCH" in error_msg
    assert "Query()" in error_msg
    assert "simple types" in error_msg


# ============================================================================
# Run all tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
