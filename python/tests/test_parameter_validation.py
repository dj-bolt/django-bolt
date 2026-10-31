"""
Test parameter validation and inference system.
"""

from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.params import Body, Param, Query


class UserCreate(msgspec.Struct):
    name: str
    email: str


def test_get_with_body_param_raises_error():
    """Test that GET endpoints with body parameters raise TypeError."""
    api = BoltAPI()

    with pytest.raises(TypeError) as exc_info:

        @api.get("/users")
        async def create_user(user: UserCreate):
            return {"id": 1}

    error_msg = str(exc_info.value)
    assert "GET /users cannot have body parameters" in error_msg
    assert "user" in error_msg
    assert "Change HTTP method to POST/PUT/PATCH" in error_msg


def test_head_with_body_param_raises_error():
    """Test that HEAD endpoints cannot have body parameters."""
    # Note: BoltAPI doesn't have @api.head() decorator yet, but we test the principle
    # This would be the expected behavior if we add HEAD support
    pass


def test_delete_with_body_param_raises_error():
    """Test that DELETE endpoints with body parameters raise TypeError."""
    api = BoltAPI()

    with pytest.raises(TypeError) as exc_info:

        @api.delete("/users/{user_id}")
        async def delete_user(user_id: int, data: UserCreate):
            return {"deleted": True}

    error_msg = str(exc_info.value)
    assert "DELETE" in error_msg
    assert "cannot have body parameters" in error_msg


def test_post_with_body_param_works():
    """Test that POST endpoints with body parameters work fine."""
    api = BoltAPI()

    # This should NOT raise an error
    @api.post("/users")
    async def create_user(user: UserCreate):
        return {"id": 1}

    # Verify the route was registered
    assert len(api._routes) == 1
    assert api._routes[0][0] == "POST"


def test_get_with_explicit_query_marker_works():
    """Test that GET with explicit Query() marker works."""
    api = BoltAPI()

    @api.get("/users")
    async def list_users(name: str = Query()):
        return {"users": []}

    assert len(api._routes) == 1


def test_get_with_simple_types_inferred_as_query():
    """Test that simple types are auto-inferred as query parameters."""
    api = BoltAPI()

    @api.get("/users")
    async def list_users(page: int = 1, limit: int = 10, search: str = ""):
        return {"users": []}

    assert len(api._routes) == 1

    # Check that parameters were inferred as query params
    handler_id = api._routes[0][2]
    meta = api._handler_meta[handler_id]

    for field in meta["fields"]:
        if field.name in ("page", "limit", "search"):
            assert field.source == "query", f"{field.name} should be query param"


def test_post_with_struct_inferred_as_body():
    """Test that msgspec.Struct is auto-inferred as body parameter."""
    api = BoltAPI()

    @api.post("/users")
    async def create_user(user: UserCreate):
        return {"id": 1}

    handler_id = api._routes[0][2]
    meta = api._handler_meta[handler_id]

    user_field = next(f for f in meta["fields"] if f.name == "user")
    assert user_field.source == "body"


def test_path_params_inferred_correctly():
    """Test that path parameters are correctly inferred."""
    api = BoltAPI()

    @api.get("/users/{user_id}/posts/{post_id}")
    async def get_post(user_id: int, post_id: int):
        return {"user_id": user_id, "post_id": post_id}

    handler_id = api._routes[0][2]
    meta = api._handler_meta[handler_id]

    user_id_field = next(f for f in meta["fields"] if f.name == "user_id")
    post_id_field = next(f for f in meta["fields"] if f.name == "post_id")

    assert user_id_field.source == "path"
    assert post_id_field.source == "path"


def test_mixed_params_inference():
    """Test mixed parameter types with auto-inference."""
    api = BoltAPI()

    @api.get("/users/{user_id}")
    async def get_user(user_id: int, include_posts: bool = False):
        return {"user_id": user_id, "include_posts": include_posts}

    handler_id = api._routes[0][2]
    meta = api._handler_meta[handler_id]

    user_id_field = next(f for f in meta["fields"] if f.name == "user_id")
    include_posts_field = next(f for f in meta["fields"] if f.name == "include_posts")

    assert user_id_field.source == "path"
    assert include_posts_field.source == "query"


def test_explicit_body_marker_with_post():
    """Test explicit Body() marker works with POST."""
    api = BoltAPI()

    @api.post("/users")
    async def create_user(user: UserCreate = Body()):
        return {"id": 1}

    handler_id = api._routes[0][2]
    meta = api._handler_meta[handler_id]

    user_field = next(f for f in meta["fields"] if f.name == "user")
    assert user_field.source == "body"


def test_error_message_clarity():
    """Test that error messages are clear and helpful."""
    api = BoltAPI()

    with pytest.raises(TypeError) as exc_info:

        @api.get("/items")
        async def bad_handler(item: UserCreate):
            pass

    error_msg = str(exc_info.value)
    # Check all parts of the error message
    assert "bad_handler" in error_msg
    assert "GET /items" in error_msg
    assert "cannot have body parameters" in error_msg
    assert "['item']" in error_msg
    assert "Solutions:" in error_msg
    assert "POST/PUT/PATCH" in error_msg
    assert "Query()" in error_msg


@pytest.mark.parametrize("default", [None, 5], ids=["required", "with-default"])
def test_unknown_parameter_source_raises_at_registration(default):
    """A marker with a source that Bolt does not know stops the route registration."""
    api = BoltAPI()

    with pytest.raises(TypeError, match="Parameter 'value' has an unsupported source 'cookies'"):

        @api.get("/items")
        async def handler(value: Annotated[int, Param(source="cookies")] = default):
            return {"value": value}
