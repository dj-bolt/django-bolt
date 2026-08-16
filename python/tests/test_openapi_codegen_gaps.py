"""
Failing tests for the schema/codegen gaps reported in #295 (DRF → Bolt migration).

Each section pins the *expected* public contract for one sub-issue so the fix is
test-driven (RED first):

- #287: parametrized generic Serializers (``Page[UserRead]``) must render as a
  named component (msgspec naming: ``Page_UserRead_``), not ``{"type": "object"}``.
- #288: a pagination class can own its envelope via ``response_class`` +
  ``build_response()``; both the wire body and the schema follow it.
- #289: ``include_in_schema=False`` on route decorators; ``response_class`` is
  honored by the schema generator (media type + schema).
- #290: ``OpenAPIConfig(strict=True)`` fails schema generation for opaque
  responses and for components that needed the qualified-name fallback.
"""

from __future__ import annotations

import types
from typing import Generic, TypeVar

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import OpenAPIStrictError, SchemaGenerator
from django_bolt.pagination import PageNumberPagination, paginate
from django_bolt.responses import (
    HTML,
    EventSourceResponse,
    File,
    PlainText,
    Redirect,
    StreamingResponse,
)
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient

from .test_models import Article

T = TypeVar("T")


class UserRead(Serializer):
    id: int
    name: str


class Page(Serializer, Generic[T]):  # noqa: UP046 — PEP 695 params break Serializer under PEP 563
    """A page of results."""

    count: int
    results: list[T]


class StructPage(msgspec.Struct, Generic[T]):  # noqa: UP046
    count: int
    results: list[T]


class ArticleListSchema(msgspec.Struct):
    id: int
    title: str


def _get_schema(api: BoltAPI) -> dict:
    api._register_openapi_routes()
    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")
        assert response.status_code == 200
        return response.json()


def _config(**kwargs) -> OpenAPIConfig:
    return OpenAPIConfig(title="Test API", version="1.0.0", **kwargs)


def _ok_response(schema: dict, path: str, method: str = "get", status: str = "200") -> dict:
    return schema["paths"][path][method]["responses"][status]


# ---------------------------------------------------------------------------
# #287 — parametrized generic Serializers / Structs
# ---------------------------------------------------------------------------


def test_generic_serializer_response_renders_named_component():
    api = BoltAPI(openapi_config=_config())

    @api.get("/users")
    async def list_users() -> Page[UserRead]:
        return Page(count=1, results=[UserRead(id=1, name="a")])

    schema = _get_schema(api)
    body = _ok_response(schema, "/users")["content"]["application/json"]["schema"]

    # Not the opaque fallback — a $ref to a component named like msgspec does.
    assert body == {"$ref": "#/components/schemas/Page_UserRead_"}

    component = schema["components"]["schemas"]["Page_UserRead_"]
    assert component["type"] == "object"
    assert component["title"] == "Page[UserRead]"
    assert component["description"] == "A page of results."
    assert component["properties"]["count"] == {"type": "integer"}
    assert component["properties"]["results"] == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/UserRead"},
    }
    assert set(component["required"]) == {"count", "results"}
    # The item type is registered as its own component too.
    assert schema["components"]["schemas"]["UserRead"]["properties"]["name"] == {"type": "string"}


def test_generic_struct_two_parametrizations_are_distinct_components():
    api = BoltAPI(openapi_config=_config())

    @api.get("/users")
    async def list_users() -> StructPage[UserRead]:
        return StructPage(count=0, results=[])

    @api.get("/ids")
    async def list_ids() -> StructPage[int]:
        return StructPage(count=0, results=[])

    schema = _get_schema(api)
    components = schema["components"]["schemas"]

    assert _ok_response(schema, "/users")["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/StructPage_UserRead_"
    }
    assert _ok_response(schema, "/ids")["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/StructPage_int_"
    }
    assert components["StructPage_int_"]["properties"]["results"] == {"type": "array", "items": {"type": "integer"}}
    assert components["StructPage_UserRead_"]["properties"]["results"]["items"] == {
        "$ref": "#/components/schemas/UserRead"
    }


def test_generic_serializer_round_trips_on_the_wire():
    """The runtime already handles generics; pin it so the schema fix cannot regress it."""
    api = BoltAPI(openapi_config=_config())

    @api.get("/users")
    async def list_users() -> Page[UserRead]:
        return Page(count=1, results=[UserRead(id=1, name="a")])

    with TestClient(api) as client:
        response = client.get("/users")
        assert response.status_code == 200
        assert response.json() == {"count": 1, "results": [{"id": 1, "name": "a"}]}


def test_paginated_route_documents_the_envelope_not_a_bare_list():
    """``@paginate`` returns ``PaginatedResponse[T]`` on the wire, so the schema must say so."""
    api = BoltAPI(openapi_config=_config())

    @api.get("/users")
    @paginate(PageNumberPagination)
    async def list_users(request) -> list[UserRead]:
        return [UserRead(id=1, name="a")]

    schema = _get_schema(api)
    body = _ok_response(schema, "/users")["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/PaginatedResponse_UserRead_"}

    envelope = schema["components"]["schemas"]["PaginatedResponse_UserRead_"]
    assert envelope["properties"]["items"] == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/UserRead"},
    }
    assert envelope["properties"]["total"] == {"type": "integer"}
    assert {"items", "total", "has_next", "has_previous"} <= set(envelope["required"])


# ---------------------------------------------------------------------------
# #288 — custom pagination envelope
# ---------------------------------------------------------------------------


class DRFPage(Serializer, Generic[T]):  # noqa: UP046
    count: int
    next: str | None
    previous: str | None
    results: list[T]


class DRFPagination(PageNumberPagination):
    page_size = 2
    response_class = DRFPage

    def build_response(self, items, *, total, request, page, page_size, has_next, has_previous, **_info):
        base = f"/articles?page_size={page_size}&page="
        return DRFPage(
            count=total,
            next=f"{base}{page + 1}" if has_next else None,
            previous=f"{base}{page - 1}" if has_previous else None,
            results=items,
        )


@pytest.fixture
def five_articles(db):
    return [
        Article.objects.create(title=f"Article {i}", content="c", author="a", is_published=True) for i in range(1, 6)
    ]


@pytest.mark.django_db(transaction=True)
def test_custom_pagination_envelope_on_the_wire(five_articles):
    api = BoltAPI(openapi_config=_config())

    @api.get("/articles")
    @paginate(DRFPagination)
    async def list_articles(request) -> list[ArticleListSchema]:
        return Article.objects.order_by("id")

    with TestClient(api) as client:
        response = client.get("/articles?page=2")
        assert response.status_code == 200
        data = response.json()

    assert set(data) == {"count", "next", "previous", "results"}
    assert data["count"] == 5
    assert data["next"] == "/articles?page_size=2&page=3"
    assert data["previous"] == "/articles?page_size=2&page=1"
    assert [a["title"] for a in data["results"]] == ["Article 3", "Article 4"]
    # Items are still serialized through the item schema (projection, not full model).
    assert set(data["results"][0]) == {"id", "title"}


def test_custom_pagination_envelope_in_schema():
    api = BoltAPI(openapi_config=_config())

    @api.get("/articles")
    @paginate(DRFPagination)
    async def list_articles(request) -> list[ArticleListSchema]:
        return Article.objects.all()

    schema = _get_schema(api)
    body = _ok_response(schema, "/articles")["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/DRFPage_ArticleListSchema_"}

    envelope = schema["components"]["schemas"]["DRFPage_ArticleListSchema_"]
    assert set(envelope["properties"]) == {"count", "next", "previous", "results"}
    assert envelope["properties"]["results"] == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/ArticleListSchema"},
    }
    # No trace of the default envelope.
    assert "PaginatedResponse_ArticleListSchema_" not in schema["components"]["schemas"]


# ---------------------------------------------------------------------------
# #289 — include_in_schema + response_class media types
# ---------------------------------------------------------------------------


def test_include_in_schema_false_hides_route_but_still_serves_it():
    api = BoltAPI(openapi_config=_config())

    @api.get("/public")
    async def public() -> UserRead:
        return UserRead(id=1, name="a")

    @api.get("/internal", include_in_schema=False)
    async def internal() -> UserRead:
        return UserRead(id=2, name="b")

    schema = _get_schema(api)
    assert "/public" in schema["paths"]
    assert "/internal" not in schema["paths"]

    with TestClient(api) as client:
        assert client.get("/internal").json() == {"id": 2, "name": "b"}


def test_include_in_schema_false_hides_route_in_prefixed_api():
    api = BoltAPI(prefix="/api/v1", openapi_config=_config())

    @api.get("/internal", include_in_schema=False)
    async def internal():
        return {"ok": True}

    schema = _get_schema(api)
    assert not any(path.endswith("/internal") for path in schema["paths"])


@pytest.mark.parametrize(
    ("response_class", "media_type", "expected_schema"),
    [
        (HTML, "text/html", {"type": "string"}),
        (PlainText, "text/plain", {"type": "string"}),
        (File, "application/octet-stream", {"type": "string", "format": "binary"}),
        (StreamingResponse, "application/octet-stream", {"type": "string", "format": "binary"}),
        (EventSourceResponse, "text/event-stream", {"type": "string"}),
    ],
    ids=["html", "plaintext", "file", "streaming", "sse"],
)
def test_response_class_sets_media_type_instead_of_json_object(response_class, media_type, expected_schema):
    api = BoltAPI(openapi_config=_config())

    @api.get("/thing", response_class=response_class)
    async def thing():
        pass

    schema = _get_schema(api)
    content = _ok_response(schema, "/thing")["content"]
    assert list(content) == [media_type]
    assert content[media_type]["schema"] == expected_schema


def test_response_class_redirect_documents_3xx_without_content():
    api = BoltAPI(openapi_config=_config())

    @api.get("/go", response_class=Redirect)
    async def go():
        return Redirect("/elsewhere")

    schema = _get_schema(api)
    responses = schema["paths"]["/go"]["get"]["responses"]
    assert "200" not in responses
    assert "content" not in responses["307"]
    assert responses["307"]["headers"]["Location"]["schema"] == {"type": "string"}


# ---------------------------------------------------------------------------
# #290 — strict mode
# ---------------------------------------------------------------------------


def test_strict_mode_fails_on_opaque_json_response():
    api = BoltAPI(openapi_config=_config(strict=True))

    @api.get("/typed")
    async def typed() -> UserRead:
        return UserRead(id=1, name="a")

    @api.get("/untyped")
    async def untyped():
        return {"a": 1}

    with pytest.raises(OpenAPIStrictError) as exc_info:
        SchemaGenerator(api, api._openapi_config).generate()

    message = str(exc_info.value)
    assert "GET /untyped" in message
    assert "/typed" not in message


def test_strict_mode_reports_every_offender_at_once():
    api = BoltAPI(openapi_config=_config(strict=True))

    @api.get("/a")
    async def a():
        return {}

    @api.post("/b")
    async def b():
        return {}

    with pytest.raises(OpenAPIStrictError) as exc_info:
        SchemaGenerator(api, api._openapi_config).generate()

    message = str(exc_info.value)
    assert "GET /a" in message
    assert "POST /b" in message


def test_strict_mode_ignores_routes_excluded_from_schema():
    api = BoltAPI(openapi_config=_config(strict=True))

    @api.get("/untyped", include_in_schema=False)
    async def untyped():
        return {"a": 1}

    @api.get("/pixel.gif", response_class=File)
    async def pixel():
        pass

    # Neither is an opaque JSON object: one is hidden, one is documented as binary.
    SchemaGenerator(api, api._openapi_config).generate()


def test_strict_mode_fails_on_qualified_component_name_fallback():
    first = types.new_class("Item", (msgspec.Struct,), {}, lambda ns: ns.update({"__annotations__": {"id": int}}))
    first.__module__ = "app_one.schemas"
    second = types.new_class("Item", (msgspec.Struct,), {}, lambda ns: ns.update({"__annotations__": {"name": str}}))
    second.__module__ = "app_two.schemas"

    api = BoltAPI(openapi_config=_config(strict=True))

    @api.get("/one", response_model=first)
    async def one():
        pass

    @api.get("/two", response_model=second)
    async def two():
        pass

    with pytest.raises(OpenAPIStrictError) as exc_info:
        SchemaGenerator(api, api._openapi_config).generate()

    message = str(exc_info.value)
    assert "app_one.schemas.Item" in message
    assert "app_two.schemas.Item" in message


def test_non_strict_mode_keeps_generating_for_opaque_responses():
    """Default stays permissive: existing apps with untyped handlers keep working."""
    api = BoltAPI(openapi_config=_config())

    @api.get("/untyped")
    async def untyped():
        return {"a": 1}

    schema = _get_schema(api)
    assert _ok_response(schema, "/untyped")["content"]["application/json"]["schema"] == {"type": "object"}
