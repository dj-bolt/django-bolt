"""
Tests for per-status-code response schemas (multi-response mode).

This test suite verifies that response_model can accept a dict mapping
status codes to response types, enabling per-status-code validation
and OpenAPI documentation.
"""

from __future__ import annotations

import asyncio
import threading

import msgspec
import pytest

from django_bolt import BoltAPI, Depends
from django_bolt.openapi.config import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.responses import JSON
from django_bolt.serialization import (
    serialize_response,
    serialize_response_sync,
)
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient
from tests.test_models import Article

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

    status, _resp_meta, _kind, body = await serialize_response({"id": 1, "name": "Test"}, meta)
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
# 6. Unmapped status code is sent as-is (no schema check)
# ============================================================================


def test_unmapped_status_code_is_sent_as_is():
    """Unmapped status code with no ellipsis catch-all encodes the body as-is."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    async def get_item(item_id: int):
        return 418, {"detail": "Unknown"}

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 418
        assert r.json() == {"detail": "Unknown"}


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

    status, _resp_meta, _kind, body = serialize_response_sync([{"id": 1, "name": "Alice"}], meta)
    assert status == 200
    decoded = msgspec.json.decode(body)
    assert decoded == [{"id": 1, "name": "Alice"}]


# ============================================================================
# Per-status bodies are not validated
# ============================================================================


def test_mismatched_body_is_not_validated():
    """Data that doesn't match the declared schema is still sent as-is."""
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 400: ErrorSchema})
    async def get_item(item_id: int):
        return 200, {"wrong_field": "no id or name"}

    with TestClient(api) as client:
        r = client.get("/items/1")
        assert r.status_code == 200
        assert r.json() == {"wrong_field": "no id or name"}


# ============================================================================
# An undeclared status in a sync (status, data) tuple keeps that status
# ============================================================================


@pytest.mark.parametrize(
    ("path", "content_type", "body"),
    [
        ("/text", "text/plain; charset=utf-8", b"teapot"),
        ("/dict", "application/json", b'{"a":1}'),
    ],
)
def test_sync_tuple_with_an_undeclared_status_keeps_that_status(path, content_type, body):
    """The catch-all entry encodes the data, and the tuple status stays.

    The status of the catch-all entry is the route default, 200. A declared
    status cannot show the difference, because its entry has that status.
    """
    api = BoltAPI()

    @api.get("/text", response_model={200: OkSchema})
    def text():
        return 418, "teapot"

    @api.get("/dict", response_model={200: OkSchema})
    def data():
        return 418, {"a": 1}

    with TestClient(api) as client:
        response = client.get(path)

    assert response.status_code == 418
    assert response.headers["content-type"] == content_type
    assert response.content == body


# ============================================================================
# An explicit status_code that the dict does not list
# ============================================================================


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_bare_return_has_an_explicit_status_code_that_the_dict_does_not_list(is_async):
    """The catch-all entry serves the bare return, with the status of the route."""
    api = BoltAPI()

    if is_async:

        @api.post("/items", response_model={200: OkSchema, ...: ErrorSchema}, status_code=201)
        async def create_item():
            return {"id": 1, "name": "New"}

    else:

        @api.post("/items", response_model={200: OkSchema, ...: ErrorSchema}, status_code=201)
        def create_item():
            return {"id": 1, "name": "New"}

    with TestClient(api) as client:
        response = client.post("/items")

    assert (response.status_code, response.json()) == (201, {"id": 1, "name": "New"})


class TitleRow(msgspec.Struct):
    title: str


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_bare_queryset_uses_the_type_of_the_default_status(is_async):
    """The default status is not first in the dict. Its type selects the fields."""
    api = BoltAPI()

    if is_async:

        @api.get("/articles", response_model={404: ErrorSchema, 200: list[TitleRow]})
        async def articles():
            return Article.objects.all()

    else:

        @api.get("/articles", response_model={404: ErrorSchema, 200: list[TitleRow]})
        def articles():
            return Article.objects.all()

    Article.objects.create(title="One", content="First", author="A")
    with TestClient(api) as client:
        response = client.get("/articles")

    assert (response.status_code, response.json()) == (200, [{"title": "One"}])


# ============================================================================
# The executor of each kind of handler
# ============================================================================


def register_multi_routes(api: BoltAPI, prefix: str, is_async: bool) -> None:
    """Register multi-response routes, sync or async, under ``prefix``."""
    # 203 has a plain type: its entry encodes the data with no conversion.
    responses = {200: OkSchema, 202: None, 203: list[dict]}

    if is_async:

        @api.get(f"{prefix}/no-type", response_model=responses)
        async def no_type():
            # 202 has no type, so the data is not sent.
            return 202, {"ignored": True}

        @api.get(f"{prefix}/no-data", response_model=responses)
        async def no_data():
            return 202, None

        @api.get(f"{prefix}/list", response_model=responses)
        async def listing():
            return 203, [{"id": 1, "name": "One"}]

        @api.get(f"{prefix}/args/{{item_id}}", response_model=responses)
        async def args(item_id: int, *, name: str):
            return 200, {"id": item_id, "name": name}

    else:

        @api.get(f"{prefix}/no-type", response_model=responses)
        def no_type():
            return 202, {"ignored": True}

        @api.get(f"{prefix}/no-data", response_model=responses)
        def no_data():
            return 202, None

        @api.get(f"{prefix}/list", response_model=responses)
        def listing():
            return 203, [{"id": 1, "name": "One"}]

        @api.get(f"{prefix}/args/{{item_id}}", response_model=responses)
        def args(item_id: int, *, name: str):
            return 200, {"id": item_id, "name": name}


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize(
    ("path", "status", "body"),
    [
        ("/no-type", 202, b""),
        ("/no-data", 202, b""),
        ("/list", 203, b'[{"id":1,"name":"One"}]'),
        ("/args/7?name=Seven", 200, b'{"id":7,"name":"Seven"}'),
    ],
)
def test_a_multi_response_tuple_keeps_its_status_and_body(kind, path, status, body):
    api = BoltAPI()
    register_multi_routes(api, f"/{kind}", kind == "async")

    with TestClient(api) as client:
        response = client.get(f"/{kind}{path}")

    assert (response.status_code, response.content) == (status, body)


async def async_name() -> str:
    await asyncio.sleep(0)
    return "from-async-dependency"


def test_a_multi_response_route_awaits_an_async_dependency():
    api = BoltAPI()

    @api.get("/items/{item_id}", response_model={200: OkSchema, 404: ErrorSchema})
    async def get_item(item_id: int, name: str = Depends(async_name)):
        return 200, {"id": item_id, "name": name}

    with TestClient(api) as client:
        response = client.get("/items/3")

    assert response.json() == {"id": 3, "name": "from-async-dependency"}


def test_a_multi_response_route_with_a_missing_query_parameter_gets_422():
    """Rust binds no argument, and the injector reports the missing parameter."""
    api = BoltAPI()

    @api.get("/items", response_model={200: OkSchema, 404: ErrorSchema})
    async def get_item(item_id: int):
        return 200, {"id": item_id, "name": "One"}

    with TestClient(api) as client:
        missing = client.get("/items")
        found = client.get("/items", params={"item_id": 4})

    assert missing.status_code == 422, missing.text
    assert found.json() == {"id": 4, "name": "One"}


def thread_name() -> str:
    return threading.current_thread().name


@pytest.mark.django_db(transaction=True)
def test_a_blocking_multi_response_handler_runs_with_its_dependencies_on_one_pool_thread():
    """A sync handler that uses the ORM runs on the pool. Its sync dependencies run on that thread too."""
    api = BoltAPI()

    @api.get("/count", response_model={200: dict})
    def count(label: str, dependency_thread: str = Depends(thread_name)):
        return 200, {
            "count": Article.objects.count(),
            "label": label,
            "dependency": dependency_thread,
            "handler": threading.current_thread().name,
        }

    @api.get("/count/{item_id}", response_model={200: dict})
    def count_item(item_id: int, *, label: str):
        return 200, {
            "count": Article.objects.count(),
            "item": [item_id, label],
            "handler": threading.current_thread().name,
        }

    with TestClient(api) as client:
        with_dependency = client.get("/count", params={"label": "all"}).json()
        with_arguments = client.get("/count/5", params={"label": "five"}).json()

    assert (with_dependency["count"], with_dependency["label"]) == (0, "all")
    assert with_dependency["handler"].startswith("bolt_default"), with_dependency
    assert with_dependency["dependency"] == with_dependency["handler"]
    assert with_arguments["item"] == [5, "five"]
    assert with_arguments["handler"].startswith("bolt_default"), with_arguments


def test_a_multi_response_handler_that_does_not_block_runs_off_the_pool():
    api = BoltAPI()

    @api.get("/thread", response_model={200: dict})
    def thread():
        return 200, {"handler": threading.current_thread().name}

    with TestClient(api) as client:
        response = client.get("/thread")

    assert not response.json()["handler"].startswith("bolt_default"), response.json()


class ArticleSerializer(Serializer):
    id: int
    title: str


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_serializer_entry_converts_model_instances(is_async):
    """A Serializer type builds a converter even when the entry does not validate."""
    api = BoltAPI()
    responses = {200: list[ArticleSerializer], 404: ErrorSchema}

    if is_async:

        @api.get("/tuple", response_model=responses)
        async def as_tuple():
            return 200, [article async for article in Article.objects.all()]

        @api.get("/json", response_model=responses)
        async def as_json():
            return JSON([article async for article in Article.objects.all()], status_code=200)

    else:

        @api.get("/tuple", response_model=responses)
        def as_tuple():
            return 200, list(Article.objects.all())

        @api.get("/json", response_model=responses)
        def as_json():
            return JSON(list(Article.objects.all()), status_code=200)

    article = Article.objects.create(title="One", content="First", author="A")
    with TestClient(api) as client:
        tuple_response = client.get("/tuple")
        json_response = client.get("/json")

    expected = [{"id": article.id, "title": "One"}]
    assert (tuple_response.status_code, tuple_response.json()) == (200, expected), tuple_response.text
    assert (json_response.status_code, json_response.json()) == (200, expected), json_response.text
