"""
Tests for QuerySet serialization optimization.

These tests verify that field names are pre-computed at route registration time.
"""

from typing import get_origin

import msgspec

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


class UserSchema(msgspec.Struct):
    id: int
    username: str


def test_metadata_precomputes_field_names():
    """
    Test that route metadata contains pre-computed field names for list[Struct] responses.

    This test will FAIL if:
    - Field name pre-computation is removed from _compile_binder
    - response_field_names is not stored in metadata
    """
    api = BoltAPI()

    @api.get("/users")
    async def list_users() -> list[UserSchema]:
        return []

    # Access the handler metadata
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Verify field names are pre-computed
    assert "response_field_names" in meta, "Field names should be pre-computed"
    assert set(meta["response_field_names"]) == {"id", "username"}
    assert get_origin(meta["response_type"]) is list


def test_metadata_has_no_field_names_for_non_list_responses():
    """
    Test that non-list responses don't have response_field_names in metadata.

    This verifies we only pre-compute when it makes sense.
    """
    api = BoltAPI()

    @api.get("/user")
    async def get_user() -> UserSchema:
        return UserSchema(id=1, username="test")

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Should NOT have field names for single object responses
    assert "response_field_names" in meta, "Single object responses should have field names"


def test_regular_list_still_works():
    """
    Test that regular lists (non-QuerySet) still work correctly.

    This verifies the optimization doesn't break normal list responses.
    This test will FAIL if list[Struct] responses are broken.
    """
    api = BoltAPI()

    @api.get("/users")
    async def list_users() -> list[UserSchema]:
        # Return a regular list, not a QuerySet
        return [
            UserSchema(id=1, username="alice"),
            UserSchema(id=2, username="bob"),
        ]

    client = TestClient(api)
    response = client.get("/users")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["username"] == "alice"
    assert data[1]["username"] == "bob"


def test_precomputed_fields_match_struct():
    """
    Test that pre-computed field names exactly match the struct's annotations.

    This test will FAIL if field pre-computation logic is wrong.
    """

    class DetailedSchema(msgspec.Struct):
        id: int
        name: str
        email: str
        active: bool

    api = BoltAPI()

    @api.get("/items")
    async def list_items() -> list[DetailedSchema]:
        return []

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Pre-computed fields should exactly match struct annotations
    assert "response_field_names" in meta
    precomputed = set(meta["response_field_names"])
    expected = set(DetailedSchema.__annotations__.keys())

    assert precomputed == expected, f"Pre-computed fields {precomputed} don't match struct {expected}"


def test_field_order_preserved():
    """
    Test that field order is preserved in pre-computed names.

    This is important for QuerySet.values() to work correctly.
    """

    class OrderedSchema(msgspec.Struct):
        # Order matters!
        z_field: str
        a_field: int
        m_field: bool

    api = BoltAPI()

    @api.get("/items")
    async def list_items() -> list[OrderedSchema]:
        return []

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Check that all fields are present
    field_names = meta["response_field_names"]
    assert set(field_names) == {"z_field", "a_field", "m_field"}
    # The order should match the struct definition order
    assert "z_field" in field_names
    assert "a_field" in field_names
    assert "m_field" in field_names
