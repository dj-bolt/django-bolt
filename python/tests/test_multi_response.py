"""
Tests for per-status-code response schemas (multi-response mode).

This test suite verifies that response_model can accept a dict mapping
status codes to response types, enabling per-status-code validation
and OpenAPI documentation.
"""

from __future__ import annotations

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.openapi.config import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.responses import JSON
from django_bolt.serialization import (
    serialize_response,
    serialize_response_sync,
)
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient

# ============================================================================
# Test schemas
# ============================================================================


class OkSchema(Serializer):
    """OK response schema for testing."""

    id: int
    name: str


class ErrorSchema(Serializer):
    """Error response schema for testing."""

    detail: str


class CreatedSchema(Serializer):
    """Created response schema for testing."""

    id: int
    name: str
    created: bool


# ============================================================================
# 1. Tuple return (200, data) validates against OkSchema
# ============================================================================


def test_tuple_200_validates_against_ok_schema():
    """response_model={200: Ok, 400: Err} with (200, data) validates against Ok."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    async def get_item(item_id: int):
        return 200, {"id": item_id, "name": "Alice"}

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 200
        assert r.json() == {"id": 1, "name": "Alice"}


# ============================================================================
# 2. Tuple return (400, data) validates against ErrorSchema
# ============================================================================


def test_tuple_400_validates_against_error_schema():
    """response_model={200: Ok, 400: Err} with (400, data) validates against Err."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    async def get_item(item_id: int):
        return 400, {"detail": "Not valid"}

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 400
        assert r.json() == {"detail": "Not valid"}


# ============================================================================
# 3. JSON(data, status_code=404) selects 404 schema
# ============================================================================


def test_json_response_selects_schema_by_status_code():
    """JSON(data, status_code=404) selects the 404 schema."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 404: ErrorSchema})
    async def get_item(item_id: int):
        return JSON({"detail": "Not found"}, status_code=404)

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 404
        assert r.json() == {"detail": "Not found"}


# ============================================================================
# 4. Bare dict return uses default (lowest 2xx) schema
# ============================================================================


@pytest.mark.asyncio
async def test_bare_dict_uses_default_status_code():
    """Bare dict return uses default (lowest 2xx) schema."""
    api = BoltAPI()

    @api.get("/items", response_model={200: OkSchema, 400: ErrorSchema})
    async def list_items():
        return {"id": 1, "name": "Test"}

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["default_status_code"] == 200

    status, _resp_meta, _kind, body = await serialize_response(
        {"id": 1, "name": "Test"}, meta
    )
    assert status == 200
    decoded = msgspec.json.decode(body)
    assert decoded == {"id": 1, "name": "Test"}


# ============================================================================
# 5. {204: None} with (204, None) → empty body
# ============================================================================


def test_204_none_produces_empty_body():
    """{204: None} with (204, None) returns empty body."""
    api = BoltAPI()

    @api.delete("/items/{item_id}", response_model={204: None, 404: ErrorSchema})
    async def delete_item(item_id: int):
        return 204, None

    with TestClient(api) as client:
        r = client.delete("/items/1")
        assert r.status_code == 204
        assert r.content == b""


# ============================================================================
# 6. Unmapped status code returns 500 (exception caught by dispatch)
# ============================================================================


def test_unmapped_status_code_returns_500():
    """Unmapped status code with no ellipsis catch-all returns 500."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    async def get_item(item_id: int):
        return 999, {"detail": "Unknown"}

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 500


# ============================================================================
# 7. Ellipsis catch-all works
# ============================================================================


def test_ellipsis_catch_all():
    """Ellipsis catch-all matches unmapped status codes."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, ...: ErrorSchema})
    async def get_item(item_id: int):
        return 500, {"detail": "Server error"}

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 500
        assert r.json() == {"detail": "Server error"}


# ============================================================================
# 8. OpenAPI has per-status-code entries
# ============================================================================


def test_openapi_per_status_code_entries():
    """OpenAPI schema has per-status-code response entries."""
    api = BoltAPI()

    @api.post("/items", response_model={201: CreatedSchema, 400: ErrorSchema})
    async def create_item():
        pass

    config = OpenAPIConfig(title="Test", version="1.0")
    generator = SchemaGenerator(api, config)
    openapi = generator.generate()

    path_item = openapi.paths["/items"]
    responses = path_item.post.responses

    assert "201" in responses
    assert "400" in responses
    assert responses["201"].content is not None
    assert responses["400"].content is not None


# ============================================================================
# 9. OpenAPI 204 entry has no content
# ============================================================================


def test_openapi_204_no_content():
    """OpenAPI 204 response entry has no content field."""
    api = BoltAPI()

    @api.delete("/items/{item_id}", response_model={204: None, 404: ErrorSchema})
    async def delete_item(item_id: int):
        pass

    config = OpenAPIConfig(title="Test", version="1.0")
    generator = SchemaGenerator(api, config)
    openapi = generator.generate()

    path_item = openapi.paths["/items/{item_id}"]
    responses = path_item.delete.responses

    assert "204" in responses
    assert responses["204"].content is None
    assert "404" in responses
    assert responses["404"].content is not None


# ============================================================================
# 10. Sync handler works
# ============================================================================


def test_sync_handler_multi_response():
    """Sync handler with multi-response dict works end-to-end."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    def get_item(item_id: int):
        return 200, {"id": item_id, "name": "Alice"}

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 200
        assert r.json() == {"id": 1, "name": "Alice"}


def test_sync_handler_multi_response_error():
    """Sync handler with multi-response selects error schema."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    def get_item(item_id: int):
        return 400, {"detail": "Bad request"}

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 400
        assert r.json() == {"detail": "Bad request"}


def test_sync_json_response_multi():
    """Sync handler with JSON() return selects schema by status code."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 404: ErrorSchema})
    def get_item(item_id: int):
        return JSON({"detail": "Not found"}, status_code=404)

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 404
        assert r.json() == {"detail": "Not found"}


# ============================================================================
# 11. Existing response_model=Type behavior preserved
# ============================================================================


def test_existing_single_response_model_preserved():
    """Existing response_model=Type behavior is unchanged."""
    api = BoltAPI()

    @api.get("/items", response_model=OkSchema)
    def list_items():
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["response_type"] == OkSchema
    assert meta["is_multi_response"] is False
    assert "response_map" not in meta


def test_existing_annotation_response_preserved():
    """Existing return annotation behavior is unchanged."""
    api = BoltAPI()

    @api.get("/items")
    def list_items() -> OkSchema:
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["response_type"] == OkSchema
    assert meta["is_multi_response"] is False


# ============================================================================
# Registration-time metadata tests
# ============================================================================


def test_multi_response_metadata_stored_correctly():
    """Multi-response metadata is correctly stored at registration time."""
    api = BoltAPI()

    @api.post("/items", response_model={201: CreatedSchema, 400: ErrorSchema})
    async def create_item():
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["is_multi_response"] is True
    assert meta["response_map"] == {201: CreatedSchema, 400: ErrorSchema}
    assert meta["default_status_code"] == 201  # lowest 2xx
    assert meta["response_type"] == CreatedSchema  # default code's type


def test_multi_response_default_status_code_auto_detected():
    """Default status code is auto-detected as lowest 2xx code."""
    api = BoltAPI()

    @api.post("/items", response_model={201: CreatedSchema, 200: OkSchema, 400: ErrorSchema})
    async def create_item():
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Lowest 2xx is 200
    assert meta["default_status_code"] == 200


def test_multi_response_explicit_status_code():
    """Explicit status_code param overrides auto-detection."""
    api = BoltAPI()

    @api.post(
        "/items",
        response_model={201: CreatedSchema, 400: ErrorSchema},
        status_code=201,
    )
    async def create_item():
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["default_status_code"] == 201


# ============================================================================
# Edge case: ellipsis catch-all with different status codes (thread-safety)
# ============================================================================


def test_ellipsis_catch_all_different_codes():
    """Ellipsis catch-all handles multiple unmapped status codes without shared state corruption."""
    codes = [500, 503]
    call_idx = [0]

    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, ...: ErrorSchema})
    async def get_item(item_id: int):
        code = codes[call_idx[0] % 2]
        call_idx[0] += 1
        return code, {"detail": f"Error {code}"}

    with TestClient(api) as client:
        r1 = client.get("/items/1")
        assert r1.status_code == 500
        assert r1.json() == {"detail": "Error 500"}

        r2 = client.get("/items/1")
        assert r2.status_code == 503
        assert r2.json() == {"detail": "Error 503"}

    # Also verify the shared ellipsis entry was NOT mutated at registration time
    _method, _path, handler_id, _handler = api._routes[0]
    ellipsis_entry = api._handler_meta[handler_id]["_resolved_metas"][...]
    assert ellipsis_entry["default_status_code"] not in (500, 503)


# ============================================================================
# Edge case: ellipsis-only dict rejected at registration
# ============================================================================


def test_ellipsis_only_dict_raises_at_registration():
    """response_model with only ellipsis key raises ValueError at registration."""
    api = BoltAPI()

    with pytest.raises(ValueError, match="at least one integer status code"):

        @api.get("/items", response_model={...: ErrorSchema})
        async def get_items():
            pass


# ============================================================================
# Edge case: non-2xx-only dict uses lowest int code as default
# ============================================================================


def test_non_2xx_only_dict_uses_lowest_code():
    """response_model with only non-2xx codes defaults to lowest int code."""
    api = BoltAPI()

    @api.get("/items", response_model={400: ErrorSchema, 500: ErrorSchema})
    async def get_items():
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["default_status_code"] == 400


# ============================================================================
# OpenAPI: ellipsis catch-all emits "default" response
# ============================================================================


def test_openapi_ellipsis_default_response():
    """OpenAPI schema emits 'default' response entry for ellipsis catch-all."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, ...: ErrorSchema})
    async def get_item(item_id: int):
        pass

    config = OpenAPIConfig(title="Test", version="1.0")
    generator = SchemaGenerator(api, config)
    openapi = generator.generate()

    path_item = openapi.paths["/items/{item_id}"]
    responses = path_item.get.responses

    assert "200" in responses
    assert "default" in responses
    assert responses["default"].content is not None


# ============================================================================
# Edge case: bare list return in multi-response mode
# ============================================================================


@pytest.mark.asyncio
async def test_bare_list_multi_response():
    """Bare list return in multi-response mode resolves default schema."""
    api = BoltAPI()

    @api.get("/items", response_model={200: list[OkSchema], 400: ErrorSchema})
    async def list_items():
        return [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    status, _resp_meta, _kind, body = await serialize_response(
        [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}], meta
    )
    assert status == 200
    decoded = msgspec.json.decode(body)
    assert len(decoded) == 2
    assert decoded[0] == {"id": 1, "name": "Alice"}


def test_bare_list_multi_response_sync():
    """Bare list return in multi-response mode works for sync handlers."""
    api = BoltAPI()

    @api.get("/items", response_model={200: list[OkSchema], 400: ErrorSchema})
    def list_items():
        return [{"id": 1, "name": "Alice"}]

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    status, _resp_meta, _kind, body = serialize_response_sync(
        [{"id": 1, "name": "Alice"}], meta
    )
    assert status == 200
    decoded = msgspec.json.decode(body)
    assert decoded == [{"id": 1, "name": "Alice"}]


# ============================================================================
# Validation failure returns 500
# ============================================================================


def test_validation_failure_returns_500():
    """Data that doesn't match the schema for a status code returns 500."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    async def get_item(item_id: int):
        return 200, {"wrong_field": "no id or name"}

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 500
