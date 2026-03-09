"""
Integration tests for per-status-code response schemas (multi-response mode).

These tests use TestClient to route through the real Actix Web HTTP stack,
verifying end-to-end behavior of dict-based response_model.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.responses import JSON
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient

# ============================================================================
# Test schemas
# ============================================================================


class OkSchema(Serializer):
    id: int
    name: str


class ErrorSchema(Serializer):
    detail: str


class CreatedSchema(Serializer):
    id: int
    name: str
    created: bool


# ============================================================================
# 1. Tuple return (200, data) — 200 OK
# ============================================================================


def test_tuple_200_ok():
    """Tuple return (200, data) returns 200 with validated body."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 404: ErrorSchema})
    async def get_item(item_id: int):
        return 200, {"id": item_id, "name": "Alice"}

    with TestClient(api) as client:
        response = client.get("/items/1")
        assert response.status_code == 200
        assert response.json() == {"id": 1, "name": "Alice"}


# ============================================================================
# 2. Tuple return (400, data) — 400 error
# ============================================================================


def test_tuple_400_error():
    """Tuple return (400, data) returns 400 with error schema body."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    async def get_item(item_id: int):
        return 400, {"detail": "Bad request"}

    with TestClient(api) as client:
        response = client.get("/items/1")
        assert response.status_code == 400
        assert response.json() == {"detail": "Bad request"}


# ============================================================================
# 3. JSON() return with status_code=404
# ============================================================================


def test_json_response_404():
    """JSON(data, status_code=404) returns 404 with validated body."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 404: ErrorSchema})
    async def get_item(item_id: int):
        return JSON({"detail": "Not found"}, status_code=404)

    with TestClient(api) as client:
        response = client.get("/items/1")
        assert response.status_code == 404
        assert response.json() == {"detail": "Not found"}


# ============================================================================
# 4. Bare dict uses default (lowest 2xx) schema
# ============================================================================


def test_bare_dict_default():
    """Bare dict return uses default status code (200) and validates against OkSchema."""
    api = BoltAPI()

    @api.get("/items", response_model={200: OkSchema, 400: ErrorSchema})
    async def list_items():
        return {"id": 1, "name": "Test"}

    with TestClient(api) as client:
        response = client.get("/items")
        assert response.status_code == 200
        assert response.json() == {"id": 1, "name": "Test"}


# ============================================================================
# 5. 204 None — empty body
# ============================================================================


def test_204_none_empty_body():
    """(204, None) returns 204 with empty body."""
    api = BoltAPI()

    @api.delete("/items/{item_id}", response_model={204: None, 404: ErrorSchema})
    async def delete_item(item_id: int):
        return 204, None

    with TestClient(api) as client:
        response = client.delete("/items/1")
        assert response.status_code == 204
        assert response.content == b""


# ============================================================================
# 6. Ellipsis catch-all matches unmapped code
# ============================================================================


def test_ellipsis_catch_all():
    """Ellipsis catch-all validates unmapped status codes against the catch-all schema."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, ...: ErrorSchema})
    async def get_item(item_id: int):
        return 500, {"detail": "Server error"}

    with TestClient(api) as client:
        response = client.get("/items/1")
        assert response.status_code == 500
        assert response.json() == {"detail": "Server error"}


# ============================================================================
# 7. Unmapped code without catch-all returns 500
# ============================================================================


def test_unmapped_code_500():
    """Unmapped status code without ellipsis catch-all returns 500."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    async def get_item(item_id: int):
        return 999, {"detail": "Unknown"}

    with TestClient(api) as client:
        response = client.get("/items/1")
        assert response.status_code == 500


# ============================================================================
# 8. Validation failure returns 500
# ============================================================================


def test_validation_failure_500():
    """Data that doesn't match the schema returns 500."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    async def get_item(item_id: int):
        return 200, {"wrong_field": "no id or name"}

    with TestClient(api) as client:
        response = client.get("/items/1")
        assert response.status_code == 500


# ============================================================================
# 9. POST with status_code=201
# ============================================================================


def test_post_201_created():
    """POST with explicit status_code=201 and tuple return."""
    api = BoltAPI()

    @api.post(
        "/items",
        response_model={201: CreatedSchema, 400: ErrorSchema},
        status_code=201,
    )
    async def create_item():
        return 201, {"id": 1, "name": "New Item", "created": True}

    with TestClient(api) as client:
        response = client.post("/items")
        assert response.status_code == 201
        assert response.json() == {"id": 1, "name": "New Item", "created": True}


# ============================================================================
# 10. Sync handler
# ============================================================================


def test_sync_handler():
    """Sync handler with multi-response dict works end-to-end."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 404: ErrorSchema})
    def get_item(item_id: int):
        return 200, {"id": item_id, "name": "Sync Item"}

    with TestClient(api) as client:
        response = client.get("/items/42")
        assert response.status_code == 200
        assert response.json() == {"id": 42, "name": "Sync Item"}
