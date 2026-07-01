"""
Tests for response_model parameter priority and QuerySet optimization.

This test suite verifies that:
1. response_model parameter takes precedence over return annotations
2. Return annotations work as fallback when response_model not provided
3. Both syntaxes produce identical metadata (field names for QuerySet optimization)
4. Works with Serializer subclasses (django-bolt's enhanced msgspec.Struct)
5. Works with both sync and async handlers
"""

from __future__ import annotations

import inspect

import msgspec

import django_bolt.api as api_module
from django_bolt import BoltAPI
from django_bolt.serializers import Serializer
from django_bolt.typing import is_msgspec_struct


# Test serializers
class UserMini(Serializer):
    """Minimal user serializer for testing."""

    id: int
    username: str


class UserFull(Serializer):
    """Full user serializer for testing."""

    id: int
    username: str
    email: str
    first_name: str
    last_name: str


class PlainStruct(msgspec.Struct):
    """Plain msgspec.Struct for comparison."""

    id: int
    name: str


# ============================================================================
# Test 1: response_model parameter takes precedence over return annotation
# ============================================================================


def test_response_model_overrides_annotation():
    """Test that response_model parameter takes precedence over return annotation."""
    api = BoltAPI()

    @api.get("/users", response_model=list[UserMini])
    def get_users() -> list[UserFull]:  # Different annotation - should be ignored
        """Return users (response_model should override annotation)."""
        pass

    # Get metadata from registered handler
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Verify response_type is UserMini (from response_model), not UserFull (from annotation)
    assert meta["response_type"] == list[UserMini]

    # Verify field names extracted from UserMini
    assert "response_field_names" in meta
    assert set(meta["response_field_names"]) == {"id", "username"}


def test_response_model_overrides_annotation_async():
    """Test response_model precedence with async handler."""
    api = BoltAPI()

    @api.get("/users-async", response_model=list[UserMini])
    async def get_users_async() -> list[UserFull]:
        """Async version - response_model should override annotation."""
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["response_type"] == list[UserMini]
    assert "response_field_names" in meta
    assert set(meta["response_field_names"]) == {"id", "username"}


# ============================================================================
# Test 2: Return annotation works as fallback when no response_model
# ============================================================================


def test_return_annotation_fallback():
    """Test that return annotation is used when response_model not provided."""
    api = BoltAPI()

    @api.get("/users")
    def get_users() -> list[UserMini]:
        """Return users using return annotation."""
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Verify response_type is UserMini (from return annotation)
    assert meta["response_type"] == list[UserMini]

    # Verify field names extracted
    assert "response_field_names" in meta
    assert set(meta["response_field_names"]) == {"id", "username"}


def test_return_annotation_fallback_async():
    """Test return annotation fallback with async handler."""
    api = BoltAPI()

    @api.get("/users-async")
    async def get_users_async() -> list[UserMini]:
        """Async version using return annotation."""
        pass

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["response_type"] == list[UserMini]
    assert "response_field_names" in meta
    assert set(meta["response_field_names"]) == {"id", "username"}


# ============================================================================
# Test 3: Both syntaxes produce identical metadata
# ============================================================================


def test_both_syntaxes_produce_same_metadata():
    """Test that response_model and return annotation produce identical metadata."""
    api1 = BoltAPI()
    api2 = BoltAPI()

    # Syntax 1: response_model parameter
    @api1.get("/users1", response_model=list[UserMini])
    def get_users1():
        pass

    # Syntax 2: return annotation
    @api2.get("/users2")
    def get_users2() -> list[UserMini]:
        pass

    _method1, _path1, handler_id1, _handler1 = api1._routes[0]
    meta1 = api1._handler_meta[handler_id1]
    _method2, _path2, handler_id2, _handler2 = api2._routes[0]
    meta2 = api2._handler_meta[handler_id2]

    # Both should have same response_type
    assert meta1["response_type"] == meta2["response_type"]

    # Both should have same field names
    assert meta1["response_field_names"] == meta2["response_field_names"]
    assert set(meta1["response_field_names"]) == {"id", "username"}


def test_both_syntaxes_produce_same_metadata_async():
    """Test metadata equivalence with async handlers."""
    api1 = BoltAPI()
    api2 = BoltAPI()

    @api1.get("/users1", response_model=list[UserMini])
    async def get_users1():
        pass

    @api2.get("/users2")
    async def get_users2() -> list[UserMini]:
        pass

    _method1, _path1, handler_id1, _handler1 = api1._routes[0]
    meta1 = api1._handler_meta[handler_id1]
    _method2, _path2, handler_id2, _handler2 = api2._routes[0]
    meta2 = api2._handler_meta[handler_id2]

    assert meta1["response_type"] == meta2["response_type"]
    assert meta1["response_field_names"] == meta2["response_field_names"]


# ============================================================================
# Test 4: Works with Serializer subclasses
# ============================================================================


def test_serializer_subclass_recognized():
    """Test that Serializer subclasses are recognized as msgspec.Struct."""
    # Verify Serializer is a proper msgspec.Struct subclass
    assert is_msgspec_struct(UserMini)
    assert is_msgspec_struct(UserFull)
    assert is_msgspec_struct(PlainStruct)


def test_serializer_field_extraction():
    """Test field extraction from Serializer subclasses."""
    api = BoltAPI()

    @api.get("/users", response_model=list[UserFull])
    def get_users():
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Verify all UserFull fields extracted
    assert "response_field_names" in meta
    expected_fields = {"id", "username", "email", "first_name", "last_name"}
    assert set(meta["response_field_names"]) == expected_fields


def test_plain_msgspec_struct_field_extraction():
    """Test field extraction from plain msgspec.Struct."""
    api = BoltAPI()

    @api.get("/items", response_model=list[PlainStruct])
    def get_items():
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Verify PlainStruct fields extracted
    assert "response_field_names" in meta
    assert set(meta["response_field_names"]) == {"id", "name"}


# ============================================================================
# Test 5: Edge cases and validation
# ============================================================================


def test_no_response_type_specified():
    """Test when neither response_model nor return annotation provided."""
    api = BoltAPI()

    @api.get("/items")
    def get_items():  # No return annotation, no response_model
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # response_type should be None (always present for direct access optimization)
    assert meta["response_type"] is None
    assert "response_field_names" not in meta


def test_non_list_response_type():
    """Test response_model with single struct (not list)."""
    api = BoltAPI()

    @api.get("/user", response_model=UserMini)
    def get_user():
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Should have response_type but NOT field names (optimization only for list[Struct])
    assert meta["response_type"] == UserMini
    assert "response_field_names" not in meta


def test_non_struct_response_type():
    """Test response_model with non-struct type (dict, list[int], etc.)."""
    api = BoltAPI()

    @api.get("/data1", response_model=dict)
    def get_data1():
        pass

    @api.get("/data2", response_model=list[int])
    def get_data2():
        pass

    _method1, _path1, handler_id1, _handler1 = api._routes[0]
    meta1 = api._handler_meta[handler_id1]
    _method2, _path2, handler_id2, _handler2 = api._routes[1]
    meta2 = api._handler_meta[handler_id2]

    # Should have response_type but NOT field names
    assert meta1["response_type"] is dict
    assert "response_field_names" not in meta1

    assert meta2["response_type"] == list[int]
    assert "response_field_names" not in meta2


# ============================================================================
# Test 6: Metadata extraction happens at registration time
# ============================================================================


def test_metadata_extraction_at_registration():
    """Test that field extraction happens once at route registration, not per-request."""
    api = BoltAPI()
    call_count = 0

    # Monkey-patch where the function is USED (api module), not where it's defined
    # When api.py does 'from .api_compilation import extract_response_metadata',
    # it creates a reference in the api module namespace
    original_extract = api_module.extract_response_metadata

    def tracked_extract(response_type):
        nonlocal call_count
        call_count += 1
        return original_extract(response_type)

    api_module.extract_response_metadata = tracked_extract

    try:

        @api.get("/users", response_model=list[UserMini])
        def get_users():
            pass

        # Should be called exactly once during registration
        assert call_count == 1

        # Get metadata - should not trigger another call
        # Get handler function name from previous decorator
        _method, _path, handler_id, _handler = api._routes[0]
        meta = api._handler_meta[handler_id]

        assert "response_field_names" in meta

        # Still should be 1 (no additional calls)
        assert call_count == 1
    finally:
        # Restore original function
        api_module.extract_response_metadata = original_extract


# ============================================================================
# Test 7: Integration with different HTTP methods
# ============================================================================


def test_response_model_with_post():
    """Test response_model with POST endpoint."""
    api = BoltAPI()

    @api.post("/users", response_model=UserMini)
    def create_user(username: str):
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["response_type"] == UserMini
    assert meta["http_method"] == "POST"


def test_response_model_with_put():
    """Test response_model with PUT endpoint."""
    api = BoltAPI()

    @api.put("/users/{user_id}", response_model=UserMini)
    def update_user(user_id: int, username: str):
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["response_type"] == UserMini
    assert meta["http_method"] == "PUT"


def test_response_model_with_patch():
    """Test response_model with PATCH endpoint."""
    api = BoltAPI()

    @api.patch("/users/{user_id}", response_model=UserMini)
    def partial_update_user(user_id: int):
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["response_type"] == UserMini
    assert meta["http_method"] == "PATCH"


def test_response_model_with_delete():
    """Test response_model with DELETE endpoint."""
    api = BoltAPI()

    @api.delete("/users/{user_id}", response_model=dict)
    def delete_user(user_id: int):
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    assert meta["response_type"] is dict
    assert meta["http_method"] == "DELETE"


# ============================================================================
# Test 8: Verify _compile_binder() no longer handles response logic
# ============================================================================


def test_compile_binder_focused_on_parameters():
    """Test that _compile_binder() only handles parameter binding, not response logic."""
    api = BoltAPI()

    @api.get("/users/{user_id}", response_model=list[UserMini])
    def get_user(user_id: int, limit: int = 10) -> list[UserFull]:
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Verify parameter fields extracted correctly
    assert "fields" in meta
    assert len(meta["fields"]) == 2  # user_id, limit
    field_names = [f.name for f in meta["fields"]]
    assert "user_id" in field_names
    assert "limit" in field_names

    # Verify response_type set correctly (response_model, not annotation)
    assert meta["response_type"] == list[UserMini]

    # Verify field names from response_model (UserMini), not annotation (UserFull)
    assert set(meta["response_field_names"]) == {"id", "username"}


# ============================================================================
# Test 9: Complex nested scenarios
# ============================================================================


def test_nested_list_extraction():
    """Test field extraction from nested list types."""
    api = BoltAPI()

    @api.get("/users", response_model=list[UserMini])  # Using typing.List
    def get_users():
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Should work with typing.List as well
    assert "response_field_names" in meta
    assert set(meta["response_field_names"]) == {"id", "username"}


# ============================================================================
# Test 10: Verify signature preservation
# ============================================================================


def test_signature_preserved_with_response_model():
    """Test that function signature is preserved when using response_model."""
    api = BoltAPI()

    @api.get("/users", response_model=list[UserMini])
    def get_users(limit: int = 10, offset: int = 0):
        """Get users with pagination."""
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Verify signature preserved
    sig = meta["sig"]
    assert isinstance(sig, inspect.Signature)

    params = list(sig.parameters.values())
    assert len(params) == 2
    assert params[0].name == "limit"
    assert params[0].default == 10
    assert params[1].name == "offset"
    assert params[1].default == 0


def test_signature_preserved_with_annotation():
    """Test that function signature is preserved when using return annotation."""
    api = BoltAPI()

    @api.get("/users")
    def get_users(limit: int = 10, offset: int = 0) -> list[UserMini]:
        """Get users with pagination."""
        pass

    # Get handler function name from previous decorator
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta[handler_id]

    # Verify signature preserved
    sig = meta["sig"]
    params = list(sig.parameters.values())
    assert len(params) == 2
    assert params[0].name == "limit"
    assert params[1].name == "offset"
