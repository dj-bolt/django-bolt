"""
Tests for OpenAPI tags, summary, and description metadata.
"""

import pytest
from openapi_spec_validator import validate

from django_bolt import BoltAPI, Router
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.decorators import ActionHandler, action
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.openapi.spec import Tag
from django_bolt.responses import Redirect
from django_bolt.testing import TestClient
from django_bolt.views import APIView, ViewSet
from django_bolt.websocket import WebSocket


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


def test_operation_ids_are_unique_when_function_names_repeat():
    """Two routes with one function name still get different operationId values."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))
    first = Router(prefix="/first")
    second = Router(prefix="/second")

    @first.get("/items")
    async def list_items() -> dict:
        return {}

    @second.get("/items")
    async def list_items() -> dict:  # noqa: F811
        return {}

    @api.get("/third")
    async def list_items() -> dict:  # noqa: F811
        return {}

    api.include_router(first)
    api.include_router(second)
    api._register_openapi_routes()
    with TestClient(api) as client:
        document = client.get("/docs/openapi.json").json()

    operation_ids = [
        operation["operationId"] for path_item in document["paths"].values() for operation in path_item.values()
    ]
    assert len(operation_ids) == 3
    assert len(set(operation_ids)) == 3
    assert set(operation_ids) == {"get_list_items", "get_list_items_2", "get_list_items_3"}


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


def _document(api: BoltAPI) -> dict:
    """The OpenAPI document as a client reads it from the docs route."""
    api._register_openapi_routes()
    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")
    assert response.status_code == 200
    document = response.json()
    validate(document)
    return document


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "head", "options", "query"])
def test_deprecated_route_is_deprecated_in_the_document(method):
    """``deprecated=True`` on a route decorator sets ``deprecated: true`` on the operation."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @getattr(api, method)("/old", deprecated=True)
    async def old_route() -> dict:
        return {}

    @getattr(api, method)("/current")
    async def current_route() -> dict:
        return {}

    paths = _document(api)["paths"]
    assert paths["/old"][method]["deprecated"] is True
    # The default of the specification is false, so a current operation has no key.
    assert "deprecated" not in paths["/current"][method]


def test_deprecated_reaches_router_view_viewset_and_action_routes():
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))
    router = Router(prefix="/router")

    @router.get("/old", deprecated=True)
    async def old_router_route() -> dict:
        return {}

    @router.get("/current")
    async def current_router_route() -> dict:
        return {}

    api.include_router(router)

    @api.view("/old-view", deprecated=True)
    class OldView(APIView):
        async def get(self, request) -> dict:
            return {}

        async def post(self, request) -> dict:
            return {}

    @api.viewset("/old-things", deprecated=True)
    class OldViewSet(ViewSet):
        async def list(self, request):
            return []

        async def retrieve(self, request, pk: int) -> dict:
            return {}

        @action(methods=["GET"], detail=False)
        async def recent(self, request) -> dict:
            return {}

    @api.viewset("/things")
    class ThingViewSet(ViewSet):
        async def list(self, request):
            return []

        @action(methods=["GET", "POST"], detail=False, deprecated=True)
        async def legacy(self, request) -> dict:
            return {}

        @action(methods=["GET"], detail=False)
        async def recent(self, request) -> dict:
            return {}

    paths = _document(api)["paths"]
    deprecated = {
        f"{method} {path}"
        for path, item in paths.items()
        for method, operation in item.items()
        if operation.get("deprecated")
    }
    assert deprecated == {
        "get /router/old",
        "get /old-view",
        "post /old-view",
        "get /old-things",
        "get /old-things/{pk}",
        "get /old-things/recent",
        "get /things/legacy",
        "post /things/legacy",
    }
    for path in ("/router/current", "/things", "/things/recent"):
        assert "deprecated" not in paths[path]["get"]


def test_websocket_operation_has_no_deprecated_key():
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.websocket("/ws")
    async def socket(websocket: WebSocket):
        await websocket.accept()

    assert "deprecated" not in _document(api)["paths"]["/ws"]["get"]


def _websocket_operation_tags(api: BoltAPI, path: str) -> list[str] | None:
    schema = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate()
    return schema.paths[path].get.tags


def test_websocket_default_tags_only_websocket():
    """A WebSocket route without tags gets only the WebSocket tag (issue #368)."""
    api = BoltAPI()

    @api.websocket("/ws/stream")
    async def stream(websocket: WebSocket):
        await websocket.accept()

    assert _websocket_operation_tags(api, "/ws/stream") == ["WebSocket"]


def test_websocket_explicit_tags():
    """Explicit tags on a WebSocket route replace the default tag."""
    api = BoltAPI()

    @api.websocket("/ws/stream", tags=["Streaming"])
    async def stream(websocket: WebSocket):
        await websocket.accept()

    assert _websocket_operation_tags(api, "/ws/stream") == ["Streaming"]


def test_websocket_explicit_tags_survive_mount():
    """Explicit tags on a WebSocket route stay after mount."""
    child = BoltAPI()

    @child.websocket("/ws/stream", tags=["Streaming"])
    async def stream(websocket: WebSocket):
        await websocket.accept()

    api = BoltAPI()
    api.mount("/v1", child)

    assert _websocket_operation_tags(api, "/v1/ws/stream") == ["Streaming"]


def test_websocket_empty_tags_match_http():
    """An empty tag list stays empty on a WebSocket route, as on an HTTP route."""
    api = BoltAPI()

    @api.websocket("/ws/stream", tags=[])
    async def stream(websocket: WebSocket):
        await websocket.accept()

    @api.get("/items", tags=[])
    async def get_items():
        return []

    schema = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate()
    assert schema.paths["/ws/stream"].get.tags == schema.paths["/items"].get.tags == []


def test_websocket_security_with_default_authentication_classes(settings):
    """A WebSocket route that uses BOLT_AUTHENTICATION_CLASSES documents the same security as an HTTP route."""
    settings.BOLT_AUTHENTICATION_CLASSES = [JWTAuthentication(secret="test-secret")]
    api = BoltAPI()

    @api.websocket("/ws/stream", guards=[IsAuthenticated()])
    async def stream(websocket: WebSocket):
        await websocket.accept()

    @api.get("/items", guards=[IsAuthenticated()])
    async def items():
        return []

    schema = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate()
    assert schema.paths["/items"].get.security == [{"BearerAuth": []}]
    assert schema.paths["/ws/stream"].get.security == [{"BearerAuth": []}]


def test_websocket_security_of_a_guarded_route_with_no_auth_backend():
    """A guarded WebSocket route with no backend documents the JWT default, as an HTTP route does."""
    api = BoltAPI()

    @api.websocket("/ws/stream", guards=[IsAuthenticated()])
    async def stream(websocket: WebSocket):
        await websocket.accept()

    @api.websocket("/ws/open")
    async def open_stream(websocket: WebSocket):
        await websocket.accept()

    @api.get("/items", guards=[IsAuthenticated()])
    async def items():
        return []

    schema = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate()
    assert schema.paths["/items"].get.security == [{"BearerAuth": []}]
    assert schema.paths["/ws/stream"].get.security == [{"BearerAuth": []}]
    # A route with no guard gets no default backend.
    assert schema.paths["/ws/open"].get.security is None


def test_websocket_summary_and_description():
    """Explicit summary and description on a WebSocket route replace the docstring."""
    api = BoltAPI()

    @api.websocket("/ws/stream", summary="Stream prices", description="Sends each price change.")
    async def stream(websocket: WebSocket):
        """Docstring summary."""
        await websocket.accept()

    schema = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate()
    operation = schema.paths["/ws/stream"].get
    assert operation.summary == "WebSocket: Stream prices"
    assert "Sends each price change." in operation.description
    assert "Docstring summary." not in operation.description


def test_websocket_operation_documents_the_upgrade_handshake():
    """A WebSocket operation lists the upgrade headers and the 101, 400 and 403 responses."""
    api = BoltAPI()

    @api.websocket("/ws/stream")
    async def stream(websocket: WebSocket):
        await websocket.accept()

    schema = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate().to_schema()
    operation = schema["paths"]["/ws/stream"]["get"]
    assert operation["operationId"] == "websocket_stream"
    assert operation["summary"] == "WebSocket Connection"
    headers = {p["name"]: p for p in operation["parameters"]}
    assert set(headers) == {"Upgrade", "Connection"}
    assert headers["Upgrade"]["in"] == "header"
    assert headers["Upgrade"]["required"] is True
    assert headers["Upgrade"]["schema"] == {"type": "string", "enum": ["websocket"]}
    assert headers["Connection"]["schema"] == {"type": "string", "enum": ["Upgrade"]}
    assert set(operation["responses"]) == {"101", "400", "403"}
    assert set(operation["responses"]["101"]["headers"]) == {"Upgrade", "Connection"}


def test_redirect_route_documents_the_location_header():
    """A Redirect route shows a 307 response, or its own status code, with a Location header."""
    api = BoltAPI()

    @api.get("/temporary", response_class=Redirect)
    async def temporary():
        return Redirect("/new")

    @api.get("/permanent", status_code=301, response_class=Redirect)
    async def permanent():
        return Redirect("/new")

    schema = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate().to_schema()
    assert list(schema["paths"]["/temporary"]["get"]["responses"]) == ["307"]
    assert list(schema["paths"]["/permanent"]["get"]["responses"]) == ["301"]
    response = schema["paths"]["/permanent"]["get"]["responses"]["301"]
    assert response["description"] == "Moved Permanently"
    assert response["headers"]["Location"]["schema"] == {"type": "string"}


def test_websocket_include_in_schema_false():
    """A WebSocket route with include_in_schema=False is not in the schema."""
    api = BoltAPI()

    @api.websocket("/ws/hidden", include_in_schema=False)
    async def hidden(websocket: WebSocket):
        await websocket.accept()

    @api.websocket("/ws/shown")
    async def shown(websocket: WebSocket):
        await websocket.accept()

    schema = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate()
    assert "/ws/hidden" not in schema.paths
    assert "/ws/shown" in schema.paths


def test_websocket_include_in_schema_api_default_and_mount():
    """A WebSocket route takes include_in_schema from its API, also after mount."""
    child = BoltAPI(include_in_schema=False)

    @child.websocket("/ws/hidden")
    async def hidden(websocket: WebSocket):
        await websocket.accept()

    @child.websocket("/ws/shown", include_in_schema=True)
    async def shown(websocket: WebSocket):
        await websocket.accept()

    api = BoltAPI()
    api.mount("/v1", child)

    schema = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate()
    assert "/v1/ws/hidden" not in schema.paths
    assert "/v1/ws/shown" in schema.paths
    child_schema = SchemaGenerator(child, OpenAPIConfig(title="Test API", version="1.0.0")).generate()
    assert "/ws/hidden" not in child_schema.paths
