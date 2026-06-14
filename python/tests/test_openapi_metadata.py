"""
Tests for OpenAPI tags, summary, and description metadata.
"""

from django_bolt import BoltAPI
from django_bolt.decorators import ActionHandler, action
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.openapi.spec import Tag
from django_bolt.views import ViewSet


def test_route_decorator_with_tags():
    """Test that route decorators accept and store tags."""
    api = BoltAPI()

    @api.get("/items", tags=["Items", "Inventory"])
    async def get_items():
        """Get all items."""
        return []

    # Check that tags are stored in metadata
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta.get(handler_id, {})
    assert meta != {}
    assert meta.get("openapi_tags") == ["Items", "Inventory"]


def test_route_decorator_with_summary():
    """Test that route decorators accept and store summary."""
    api = BoltAPI()

    @api.get("/items", summary="Retrieve all items")
    async def get_items():
        """Get all items."""
        return []

    # Check that summary is stored in metadata
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta.get(handler_id, {})
    assert meta != {}
    assert meta.get("openapi_summary") == "Retrieve all items"


def test_route_decorator_with_description():
    """Test that route decorators accept and store description."""
    api = BoltAPI()

    @api.get("/items", description="This endpoint returns all items from the inventory.")
    async def get_items():
        """Get all items."""
        return []

    # Check that description is stored in metadata
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta.get(handler_id, {})
    assert meta != {}
    assert meta.get("openapi_description") == "This endpoint returns all items from the inventory."


def test_route_decorator_with_all_metadata():
    """Test that route decorators accept all OpenAPI metadata together."""
    api = BoltAPI()

    @api.post(
        "/items",
        tags=["Items"],
        summary="Create a new item",
        description="Creates a new item in the inventory with the provided data.",
    )
    async def create_item():
        """Create item."""
        return {"id": 1}

    # Check that all metadata is stored
    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta.get(handler_id, {})
    assert meta != {}
    assert meta.get("openapi_tags") == ["Items"]
    assert meta.get("openapi_summary") == "Create a new item"
    assert meta.get("openapi_description") == "Creates a new item in the inventory with the provided data."


def test_schema_generation_with_custom_tags():
    """Test that schema generator uses custom tags from metadata."""
    api = BoltAPI()

    @api.get("/items", tags=["Custom", "Tags"])
    async def get_items():
        """Get items."""
        return []

    config = OpenAPIConfig(title="Test API", version="1.0.0")
    generator = SchemaGenerator(api, config)
    schema = generator.generate()

    # Check that operation has custom tags
    operation = schema.paths["/items"].get
    assert operation.tags == ["Custom", "Tags"]


def test_schema_generation_with_custom_summary():
    """Test that schema generator uses custom summary from metadata."""
    api = BoltAPI()

    @api.get("/items", summary="Custom summary")
    async def get_items():
        """This is the docstring summary.

        And this is the description.
        """
        return []

    config = OpenAPIConfig(title="Test API", version="1.0.0")
    generator = SchemaGenerator(api, config)
    schema = generator.generate()

    # Custom summary should override docstring
    operation = schema.paths["/items"].get
    assert operation.summary == "Custom summary"


def test_schema_generation_with_custom_description():
    """Test that schema generator uses custom description from metadata."""
    api = BoltAPI()

    @api.get("/items", description="Custom description")
    async def get_items():
        """Docstring summary.

        Docstring description.
        """
        return []

    config = OpenAPIConfig(title="Test API", version="1.0.0")
    generator = SchemaGenerator(api, config)
    schema = generator.generate()

    # Custom description should override docstring
    operation = schema.paths["/items"].get
    assert operation.description == "Custom description"


def test_schema_generation_fallback_to_docstring():
    """Test that schema generator falls back to docstring when metadata not provided."""
    api = BoltAPI()

    @api.get("/items")
    async def get_items():
        """Get all items.

        This endpoint returns all items from the database.
        """
        return []

    config = OpenAPIConfig(title="Test API", version="1.0.0", use_handler_docstrings=True)
    generator = SchemaGenerator(api, config)
    schema = generator.generate()

    # Should use docstring
    operation = schema.paths["/items"].get
    assert operation.summary == "Get all items."
    assert operation.description == "This endpoint returns all items from the database."


def test_tag_collection_in_schema():
    """Test that schema collects tags and creates Tag objects."""
    api = BoltAPI()

    @api.get("/items", tags=["Items"])
    async def get_items():
        return []

    @api.post("/orders", tags=["Orders"])
    async def create_order():
        return {}

    @api.get("/users", tags=["Users", "Admin"])
    async def get_users():
        return []

    config = OpenAPIConfig(title="Test API", version="1.0.0")
    generator = SchemaGenerator(api, config)
    schema = generator.generate()

    # Check that tags are collected
    assert schema.tags is not None
    tag_names = {tag.name for tag in schema.tags}
    assert tag_names == {"Items", "Orders", "Users", "Admin"}


def test_tag_collection_with_config_tags():
    """Test that schema merges collected tags with config tags."""
    api = BoltAPI()

    @api.get("/items", tags=["Items"])
    async def get_items():
        return []

    # Pre-define a tag in config with description
    config = OpenAPIConfig(
        title="Test API",
        version="1.0.0",
        tags=[Tag(name="Items", description="Inventory items")],
    )
    generator = SchemaGenerator(api, config)
    schema = generator.generate()

    # Check that config tag is used (with description)
    assert schema.tags is not None
    items_tag = next(tag for tag in schema.tags if tag.name == "Items")
    assert items_tag.description == "Inventory items"


def test_action_decorator_with_metadata():
    """Test that @action decorator accepts and stores metadata."""

    @action(
        methods=["POST"],
        detail=True,
        tags=["Actions"],
        summary="Activate user",
        description="Activates the specified user account.",
    )
    async def activate(self, id: int):
        return {"activated": True}

    assert isinstance(activate, ActionHandler)
    assert activate.tags == ["Actions"]
    assert activate.summary == "Activate user"
    assert activate.description == "Activates the specified user account."


def test_action_metadata_passed_to_route():
    """Test that action metadata is passed through to the route."""
    api = BoltAPI()

    @api.viewset("/users")
    class UserViewSet(ViewSet):
        @action(
            methods=["POST"],
            detail=True,
            tags=["UserActions"],
            summary="Deactivate user",
            description="Deactivates a user account.",
        )
        async def deactivate(self, id: int):
            return {"deactivated": True}

    # Find the registered handler
    deactivate_route = None
    for _method, path, _handler_id, handler in api._routes:
        if path == "/users/{pk}/deactivate":
            deactivate_route = handler
            break

    assert deactivate_route is not None
    # Find the handler_id from routes
    for _method, path, handler_id, _handler in api._routes:
        if path == "/users/{pk}/deactivate":
            meta = api._handler_meta.get(handler_id, {})
            assert meta != {}
            assert meta.get("openapi_tags") == ["UserActions"]
            assert meta.get("openapi_summary") == "Deactivate user"
            assert meta.get("openapi_description") == "Deactivates a user account."
            break


def test_multiple_http_methods_with_metadata():
    """Test that metadata works with all HTTP methods."""
    api = BoltAPI()

    @api.get("/test", tags=["Test"], summary="GET test")
    async def get_test():
        return {}

    @api.post("/test", tags=["Test"], summary="POST test")
    async def post_test():
        return {}

    @api.put("/test", tags=["Test"], summary="PUT test")
    async def put_test():
        return {}

    @api.patch("/test", tags=["Test"], summary="PATCH test")
    async def patch_test():
        return {}

    @api.delete("/test", tags=["Test"], summary="DELETE test")
    async def delete_test():
        return {}

    # Verify all handlers have metadata
    for _method, _path, handler_id, _handler in api._routes:
        meta = api._handler_meta.get(handler_id, {})
        assert meta != {}
        assert meta.get("openapi_tags") == ["Test"]
        assert "test" in meta.get("openapi_summary", "").lower()


def test_empty_tags_not_stored():
    """Test that None/empty tags are not stored."""
    api = BoltAPI()

    @api.get("/items")
    async def get_items():
        return []

    _method, _path, handler_id, _handler = api._routes[0]
    meta = api._handler_meta.get(handler_id, {})
    assert meta != {}
    assert meta.get("openapi_tags") is None


def test_partial_metadata_override():
    """Test that only specified metadata overrides docstring."""
    api = BoltAPI()

    @api.get("/items", summary="Custom summary only")
    async def get_items():
        """Docstring summary.

        Docstring description that should be used.
        """
        return []

    config = OpenAPIConfig(title="Test API", version="1.0.0", use_handler_docstrings=True)
    generator = SchemaGenerator(api, config)
    schema = generator.generate()

    operation = schema.paths["/items"].get
    # Summary should be custom
    assert operation.summary == "Custom summary only"
    # Description should fall back to docstring
    assert operation.description == "Docstring description that should be used."
