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

import dataclasses
import types
from typing import Annotated, Generic, TypeVar

import msgspec
import pytest
from openapi_spec_validator import validate

from django_bolt import BoltAPI, ModelViewSet
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import OpenAPIStrictError, SchemaGenerator
from django_bolt.pagination import CursorPagination, LimitOffsetPagination, PageNumberPagination, paginate
from django_bolt.params import Body, Depends
from django_bolt.responses import (
    HTML,
    JSON,
    EventSourceResponse,
    File,
    FileResponse,
    PlainText,
    Redirect,
    Response,
    StreamingResponse,
)
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient

from .test_models import Article

T = TypeVar("T")


class UserRead(Serializer):
    id: int
    name: str


type UserOrJSON = UserRead | JSON
type AnyJSON = Annotated[JSON, "A JSON body."]


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
        """List the users."""
        return [UserRead(id=1, name="a")]

    schema = _get_schema(api)
    # The paginated handler keeps its docstring.
    assert schema["paths"]["/users"]["get"]["summary"] == "List the users."
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
# #917 — the query parameters of a paginated route
# ---------------------------------------------------------------------------


class SmallPages(PageNumberPagination):
    page_size = 10
    max_page_size = 50
    page_size_query_param = "size"


class FixedPageSize(CursorPagination):
    page_size_query_param = None


def _integer(**keywords) -> dict:
    return {"type": "integer", **keywords}


@pytest.mark.parametrize(
    ("pagination_class", "expected"),
    [
        (
            PageNumberPagination,
            {
                "page": _integer(minimum=1, default=1),
                "page_size": _integer(minimum=1, maximum=1000, default=100),
            },
        ),
        (
            SmallPages,
            {
                "page": _integer(minimum=1, default=1),
                "size": _integer(minimum=1, maximum=50, default=10),
            },
        ),
        (
            LimitOffsetPagination,
            {
                "limit": _integer(minimum=1, maximum=1000, default=100),
                "offset": _integer(minimum=0, default=0),
            },
        ),
        (
            CursorPagination,
            {
                "cursor": {"type": "string"},
                "page_size": _integer(minimum=1, maximum=1000, default=100),
            },
        ),
        (FixedPageSize, {"cursor": {"type": "string"}}),
    ],
)
def test_paginated_route_documents_its_query_parameters(pagination_class, expected):
    """The operation lists each query parameter that the pagination class reads."""
    api = BoltAPI(openapi_config=_config())

    @api.get("/articles")
    @paginate(pagination_class)
    async def list_articles(request, title: str | None = None) -> list[ArticleListSchema]:
        return Article.objects.order_by("id")

    parameters = _get_schema(api)["paths"]["/articles"]["get"]["parameters"]
    assert [p["name"] for p in parameters] == ["title", *expected]
    schema_parameters = {p["name"]: p for p in parameters[1:]}
    for name, schema in expected.items():
        assert schema_parameters[name]["in"] == "query"
        assert schema_parameters[name]["required"] is False
        assert schema_parameters[name]["schema"] == schema
        assert schema_parameters[name]["description"]


def test_paginated_route_keeps_a_query_parameter_of_the_handler():
    """A handler parameter with the name of a pagination parameter is documented one time."""
    api = BoltAPI(openapi_config=_config())

    @api.get("/articles")
    @paginate(PageNumberPagination)
    async def list_articles(request, page: int = 1) -> list[ArticleListSchema]:
        return Article.objects.order_by("id")

    parameters = _get_schema(api)["paths"]["/articles"]["get"]["parameters"]
    assert [p["name"] for p in parameters] == ["page", "page_size"]


def test_paginated_viewset_list_documents_its_query_parameters():
    """The list action of a viewset with ``pagination_class`` lists the pagination parameters."""
    api = BoltAPI(openapi_config=_config())

    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleListSchema
        pagination_class = LimitOffsetPagination

    schema = _get_schema(api)
    assert [p["name"] for p in schema["paths"]["/articles"]["get"]["parameters"]] == ["limit", "offset"]
    # The detail route reads no pagination parameter.
    assert [p["name"] for p in schema["paths"]["/articles/{pk}"]["get"]["parameters"]] == ["pk"]


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


def _register_with_response_class(api, path, handler, response_class, declared_by):
    """Register ``handler`` with the response class as an option, as a return annotation, or as both."""
    if declared_by != "option":
        handler.__annotations__["return"] = response_class
    api.get(path, response_class=None if declared_by == "annotation" else response_class)(handler)


@pytest.mark.parametrize("declared_by", ["option", "annotation", "both"])
@pytest.mark.parametrize(
    ("response_class", "media_type", "expected_schema"),
    [
        (HTML, "text/html", {"type": "string"}),
        (PlainText, "text/plain", {"type": "string"}),
        (File, "application/octet-stream", {"type": "string", "format": "binary"}),
        (FileResponse, "application/octet-stream", {"type": "string", "format": "binary"}),
        (StreamingResponse, "application/octet-stream", {"type": "string", "format": "binary"}),
        (EventSourceResponse, "text/event-stream", {"type": "string"}),
    ],
    ids=["html", "plaintext", "file", "file_response", "streaming", "sse"],
)
def test_response_class_sets_media_type_instead_of_json_object(
    response_class, media_type, expected_schema, declared_by
):
    api = BoltAPI(openapi_config=_config())

    async def thing():
        pass

    _register_with_response_class(api, "/thing", thing, response_class, declared_by)

    schema = _get_schema(api)
    content = _ok_response(schema, "/thing")["content"]
    assert list(content) == [media_type]
    assert content[media_type]["schema"] == expected_schema


@pytest.mark.parametrize("declared_by", ["option", "annotation", "both"])
def test_response_class_redirect_documents_3xx_without_content(declared_by):
    api = BoltAPI(openapi_config=_config())

    async def go():
        return Redirect("/elsewhere")

    _register_with_response_class(api, "/go", go, Redirect, declared_by)

    schema = _get_schema(api)
    responses = schema["paths"]["/go"]["get"]["responses"]
    assert "200" not in responses
    assert "content" not in responses["307"]
    assert responses["307"]["headers"]["Location"]["schema"] == {"type": "string"}


def _register_with_return_type(api, path, return_type, declared_by):
    """Register a route with ``return_type`` as its return annotation or as its ``response_model``."""

    async def thing():
        pass

    if declared_by == "annotation":
        thing.__annotations__["return"] = return_type
        api.get(path)(thing)
    else:
        api.get(path, response_model=return_type)(thing)


@pytest.mark.parametrize("declared_by", ["annotation", "response_model"])
@pytest.mark.parametrize(
    ("return_type", "equivalent"),
    [
        (Annotated[PlainText, "A text body."], PlainText),
        (Annotated[EventSourceResponse, "Events."] | None, EventSourceResponse),
        (UserRead | JSON, UserRead),
        (UserRead | Response | None, UserRead | None),
        (
            Annotated[UserRead | JSON, msgspec.Meta(description="A user.")],
            Annotated[UserRead, msgspec.Meta(description="A user.")],
        ),
        (Annotated[UserRead, "A user."] | JSON | None, UserRead | None),
        (UserOrJSON | None, UserRead | None),
        (UserRead | Annotated[JSON | None, "Optional."], UserRead | None),
        (Annotated[str, "A text body."], str),
    ],
    ids=[
        "annotated-text",
        "annotated-sse-or-none",
        "body-or-json",
        "body-or-response-or-none",
        "annotated-union",
        "annotated-member",
        "alias",
        "nested-none",
        "annotated-str",
    ],
)
def test_response_class_in_return_type_documents_the_body_type(return_type, equivalent, declared_by):
    """A response class names the class of the result, also in ``Annotated`` and in a union (#930).

    The route documents the media type of the class and the schema of the
    other members, as the route with only those members does.
    """
    api = BoltAPI(openapi_config=_config())
    _register_with_return_type(api, "/thing", return_type, declared_by)
    _register_with_return_type(api, "/equivalent", equivalent, declared_by)

    schema = _get_schema(api)
    validate(schema)
    assert _ok_response(schema, "/thing")["content"] == _ok_response(schema, "/equivalent")["content"]


@pytest.mark.parametrize("declared_by", ["annotation", "response_model"])
@pytest.mark.parametrize(
    ("return_type", "media_type"),
    [
        (JSON, "application/json"),
        (JSON | None, "application/json"),
        (Annotated[JSON, "A JSON body."], "application/json"),
        (Annotated[JSON, "A JSON body."] | None, "application/json"),
        (Response, "*/*"),
        (Annotated[Response | None, "Any body."], "*/*"),
        (JSON | Response, "*/*"),
        (AnyJSON, "application/json"),
    ],
    ids=[
        "json",
        "json-or-none",
        "annotated-json",
        "annotated-json-or-none",
        "response",
        "annotated-response",
        "json-or-response",
        "alias",
    ],
)
def test_response_class_with_no_body_type_documents_any_body(return_type, media_type, declared_by):
    """``-> JSON`` can send any JSON value, and ``-> Response`` any media type (#930).

    Thus the schema accepts each value. ``{"type": "object"}`` refused a list.
    """
    api = BoltAPI(openapi_config=_config())
    _register_with_return_type(api, "/thing", return_type, declared_by)

    schema = _get_schema(api)
    validate(schema)
    assert _ok_response(schema, "/thing")["content"] == {media_type: {"schema": {}}}


@pytest.mark.parametrize("return_type", [str | JSON, Annotated[str | JSON, "A text body."]], ids=["plain", "annotated"])
def test_str_or_json_documents_the_text_and_the_json_body(return_type):
    """A str result is text/plain, and ``JSON("ok")`` is an application/json string (#930)."""
    api = BoltAPI(openapi_config=_config())
    _register_with_return_type(api, "/thing", return_type, "annotation")

    schema = _get_schema(api)
    validate(schema)
    assert _ok_response(schema, "/thing")["content"] == {
        "text/plain": {"schema": {"type": "string"}},
        "application/json": {"schema": {"type": "string"}},
    }


def test_response_class_in_response_model_dict_documents_the_body_type():
    """Each status of a ``response_model`` dict documents a response class as a return annotation does (#930)."""
    api = BoltAPI(openapi_config=_config())

    @api.get("/thing", response_model={200: Annotated[JSON, "A JSON body."], 404: UserRead | JSON})
    async def thing():
        pass

    @api.get("/equivalent", response_model={404: UserRead})
    async def equivalent():
        pass

    schema = _get_schema(api)
    validate(schema)
    assert _ok_response(schema, "/thing")["content"] == {"application/json": {"schema": {}}}
    assert (
        _ok_response(schema, "/thing", status="404")["content"]
        == _ok_response(schema, "/equivalent", status="404")["content"]
    )


@pytest.mark.parametrize(
    ("return_type", "value", "media_type", "expected_schema"),
    [
        (str, "hello", "text/plain", {"type": "string"}),
        (bytes, b"\x00\x01", "application/octet-stream", {"type": "string", "format": "binary"}),
    ],
    ids=["str", "bytes"],
)
def test_str_or_bytes_return_type_documents_the_media_type_that_the_route_sends(
    return_type, value, media_type, expected_schema
):
    api = BoltAPI(openapi_config=_config())

    async def thing():
        return value

    thing.__annotations__["return"] = return_type
    api.get("/thing")(thing)
    api._register_openapi_routes()

    with TestClient(api) as client:
        content = _ok_response(client.get("/docs/openapi.json").json(), "/thing")["content"]
        sent = client.get("/thing")

    assert sent.headers["content-type"].split(";")[0] == media_type
    assert list(content) == [media_type]
    assert content[media_type]["schema"] == expected_schema


class _Problem(msgspec.Struct):
    message: str


_TEXT = {"text/plain": {"type": "string"}}
_BINARY = {"application/octet-stream": {"type": "string", "format": "binary"}}
_NULL_JSON = {"application/json": {"type": "null"}}
_PROBLEM_JSON = {"application/json": {"$ref": "#/components/schemas/_Problem"}}


@pytest.mark.parametrize(
    ("route_kwargs", "return_type", "value", "status", "sent_media_type", "expected"),
    [
        ({}, str | None, "hello", "200", "text/plain", _TEXT | _NULL_JSON),
        ({}, str | None, None, "200", "application/json", _TEXT | _NULL_JSON),
        ({}, bytes | None, b"\x00", "200", "application/octet-stream", _BINARY | _NULL_JSON),
        ({}, str | _Problem, "hello", "200", "text/plain", _TEXT | _PROBLEM_JSON),
        ({}, str | _Problem, _Problem("m"), "200", "application/json", _TEXT | _PROBLEM_JSON),
        (
            {},
            str | _Problem | None,
            None,
            "200",
            "application/json",
            _TEXT | {"application/json": {"anyOf": [_PROBLEM_JSON["application/json"], {"type": "null"}]}},
        ),
        ({"response_model": str | None}, None, "hello", "200", "text/plain", _TEXT | _NULL_JSON),
        ({"response_model": {200: str, 404: _Problem}}, None, "hello", "200", "text/plain", _TEXT),
        ({"response_model": {200: str, 404: _Problem}}, None, (404, _Problem("m")), "404", "application/json", None),
        ({"response_model": {200: _Problem, 202: str}}, None, (202, "accepted"), "202", "text/plain", _TEXT),
        ({"response_model": {200: bytes, 404: _Problem}}, None, b"\x00", "200", "application/octet-stream", _BINARY),
        ({"response_model": {404: _Problem, ...: str}}, None, (201, "made"), "default", "text/plain", _TEXT),
    ],
    ids=[
        "optional-str",
        "optional-str-none",
        "optional-bytes",
        "str-or-struct",
        "str-or-struct-struct",
        "str-or-struct-or-none",
        "response-model-optional-str",
        "map-str",
        "map-str-other-status",
        "map-second-status-str",
        "map-bytes",
        "map-catch-all-str",
    ],
)
def test_str_or_bytes_in_a_union_or_a_response_map_documents_the_media_type_that_the_route_sends(
    route_kwargs, return_type, value, status, sent_media_type, expected
):
    api = BoltAPI(openapi_config=_config())

    async def thing():
        return value

    if return_type is not None:
        thing.__annotations__["return"] = return_type
    api.get("/thing", **route_kwargs)(thing)
    api._register_openapi_routes()

    with TestClient(api) as client:
        content = _ok_response(client.get("/docs/openapi.json").json(), "/thing", status=status)["content"]
        sent = client.get("/thing")

    assert sent.headers["content-type"].split(";")[0] == sent_media_type
    assert sent_media_type in content
    documented = {media_type: media["schema"] for media_type, media in content.items()}
    assert documented == (expected or _PROBLEM_JSON)


# ---------------------------------------------------------------------------
# #945 — the msgspec.Meta of an Annotated component reference
# ---------------------------------------------------------------------------

USER_REF = {"$ref": "#/components/schemas/UserRead"}
USER_META = msgspec.Meta(
    title="The user",
    description="The user of the request.",
    examples=[{"id": 1, "name": "a"}],
    extra_json_schema={"deprecated": True},
)
USER_META_KEYWORDS = {
    "title": "The user",
    "description": "The user of the request.",
    "examples": [{"id": 1, "name": "a"}],
    "deprecated": True,
}

# ``reference`` and ``to_schema`` are attributes of the generator classes, not schema keywords.
NOT_SCHEMA_KEYWORDS_META = msgspec.Meta(description="A user.", extra_json_schema={"reference": "x", "to_schema": "y"})


def _plain_user_component() -> dict:
    """The component of ``UserRead`` in an API with no ``Meta``."""
    api = BoltAPI(openapi_config=_config())

    @api.get("/plain")
    async def plain() -> UserRead:
        pass

    return _get_schema(api)["components"]["schemas"]["UserRead"]


@pytest.mark.parametrize("declared_by", ["annotation", "response_model"])
@pytest.mark.parametrize(
    ("return_type", "expected"),
    [
        (Annotated[UserRead, USER_META], {**USER_REF, **USER_META_KEYWORDS}),
        (list[Annotated[UserRead, USER_META]], {"type": "array", "items": {**USER_REF, **USER_META_KEYWORDS}}),
        (Annotated[list[UserRead], USER_META], {"type": "array", "items": USER_REF, **USER_META_KEYWORDS}),
        (Annotated[UserRead, USER_META] | None, {"anyOf": [{**USER_REF, **USER_META_KEYWORDS}, {"type": "null"}]}),
        (Annotated[UserRead | None, USER_META], {"anyOf": [USER_REF, {"type": "null"}], **USER_META_KEYWORDS}),
    ],
    ids=["annotated", "list-of-annotated", "annotated-list", "annotated-or-none", "annotated-optional"],
)
def test_meta_of_an_annotated_response_type_is_next_to_the_component_reference(return_type, expected, declared_by):
    """``-> Annotated[Item, Meta(...)]`` keeps the Meta keywords next to the ``$ref`` (#945).

    OpenAPI 3.1 allows keywords next to ``$ref``. The shared component does not change.
    """
    api = BoltAPI(openapi_config=_config())
    _register_with_return_type(api, "/thing", return_type, declared_by)

    schema = _get_schema(api)
    validate(schema)
    assert _ok_response(schema, "/thing")["content"] == {"application/json": {"schema": expected}}
    assert schema["components"]["schemas"]["UserRead"] == _plain_user_component()


def test_meta_of_an_annotated_request_body_item_is_next_to_the_component_reference():
    """A body of the type ``list[Annotated[Item, Meta(...)]]`` keeps the Meta keywords next to each ``$ref`` (#945).

    A body of the type ``Annotated[Item, Meta(...)]`` keeps only ``Item`` when Bolt binds the parameter.
    """
    api = BoltAPI(openapi_config=_config())

    @api.post("/users")
    async def create_users(body: Annotated[list[Annotated[UserRead, USER_META]], Body()]):
        pass

    schema = _get_schema(api)
    validate(schema)
    body = schema["paths"]["/users"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"type": "array", "items": {**USER_REF, **USER_META_KEYWORDS}}
    assert schema["components"]["schemas"]["UserRead"] == _plain_user_component()


def test_each_annotated_reference_keeps_its_own_meta():
    """Two routes with a different Meta on one component each document their own Meta (#945)."""
    api = BoltAPI(openapi_config=_config())

    @api.get("/first")
    async def first() -> Annotated[UserRead, msgspec.Meta(description="The first user.")]:
        pass

    @api.get("/second")
    async def second() -> Annotated[UserRead, msgspec.Meta(description="The second user.")]:
        pass

    @api.get("/plain")
    async def plain() -> UserRead:
        pass

    schema = _get_schema(api)
    validate(schema)
    assert _ok_response(schema, "/first")["content"]["application/json"]["schema"] == {
        **USER_REF,
        "description": "The first user.",
    }
    assert _ok_response(schema, "/second")["content"]["application/json"]["schema"] == {
        **USER_REF,
        "description": "The second user.",
    }
    assert _ok_response(schema, "/plain")["content"]["application/json"]["schema"] == USER_REF
    assert schema["components"]["schemas"]["UserRead"] == _plain_user_component()


def test_extra_json_schema_sets_only_schema_keywords_next_to_the_reference():
    """A key of ``extra_json_schema`` that is not a schema keyword does not change the schema (#945).

    The generator ignores the key, next to a reference and on an inline schema.
    """
    api = BoltAPI(openapi_config=_config())

    @api.get("/thing")
    async def thing() -> Annotated[UserRead, NOT_SCHEMA_KEYWORDS_META]:
        pass

    @api.get("/numbers")
    async def numbers() -> list[Annotated[int, NOT_SCHEMA_KEYWORDS_META]]:
        pass

    schema = _get_schema(api)
    validate(schema)
    assert _ok_response(schema, "/thing")["content"]["application/json"]["schema"] == {
        **USER_REF,
        "description": "A user.",
    }
    assert _ok_response(schema, "/numbers")["content"]["application/json"]["schema"] == {
        "type": "array",
        "items": {"type": "integer", "description": "A user."},
    }


def test_annotated_reference_follows_the_qualified_component_name():
    """The ``$ref`` next to the Meta keywords uses the final name of the component (#945).

    Two classes with one name get their qualified names after the routes are read.
    """
    first = types.new_class("Item", (msgspec.Struct,), {}, lambda ns: ns.update({"__annotations__": {"id": int}}))
    first.__module__ = "app_one.schemas"
    second = types.new_class("Item", (msgspec.Struct,), {}, lambda ns: ns.update({"__annotations__": {"name": str}}))
    second.__module__ = "app_two.schemas"

    api = BoltAPI(openapi_config=_config())

    @api.get("/one", response_model=Annotated[first, msgspec.Meta(description="The first item.")])
    async def one():
        pass

    @api.get("/two", response_model=second)
    async def two():
        pass

    schema = _get_schema(api)
    validate(schema)
    assert _ok_response(schema, "/one")["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/app_one.schemas.Item",
        "description": "The first item.",
    }
    assert "app_one.schemas.Item" in schema["components"]["schemas"]


# ---------------------------------------------------------------------------
# #958 — the msgspec.Meta of an Annotated struct body
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class NewUser:
    name: str


async def _user_body_dependency(body: Annotated[UserRead, USER_META]) -> UserRead:
    return body


def _request_body_schema(schema: dict, path: str) -> dict:
    return schema["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]


def test_meta_of_an_annotated_struct_body_is_next_to_the_component_reference():
    """``body: Annotated[Item, Meta(...)]`` keeps the Meta keywords next to the ``$ref`` (#958).

    Bolt binds such a body as ``Item``. Only the schema reads the Meta.
    """
    api = BoltAPI(openapi_config=_config())

    @api.post("/users")
    async def create_user(body: Annotated[UserRead, USER_META]):
        pass

    @api.post("/embedded")
    async def create_embedded(user: Annotated[UserRead, USER_META, Body(embed=True)]):
        pass

    @api.post("/dataclass")
    async def create_dataclass(body: Annotated[NewUser, msgspec.Meta(description="A new user.")]):
        pass

    @api.post("/optional")
    async def create_optional(body: Annotated[UserRead | None, USER_META] = None):
        pass

    @api.post("/dependency")
    async def create_with_dependency(user=Depends(_user_body_dependency)):
        pass

    schema = _get_schema(api)
    validate(schema)
    assert _request_body_schema(schema, "/users") == {**USER_REF, **USER_META_KEYWORDS}
    assert _request_body_schema(schema, "/optional") == {
        "anyOf": [USER_REF, {"type": "null"}],
        **USER_META_KEYWORDS,
    }
    assert _request_body_schema(schema, "/dependency") == {**USER_REF, **USER_META_KEYWORDS}
    assert _request_body_schema(schema, "/embedded") == {
        "type": "object",
        "properties": {"user": {**USER_REF, **USER_META_KEYWORDS}},
        "required": ["user"],
    }
    assert _request_body_schema(schema, "/dataclass") == {
        "$ref": "#/components/schemas/NewUser",
        "description": "A new user.",
    }
    assert schema["components"]["schemas"]["UserRead"] == _plain_user_component()


def test_meta_of_an_annotated_struct_body_does_not_change_binding():
    """The Meta of ``body: Annotated[Item, Meta(...)]`` is for the schema only (#958).

    The body decodes as ``Item``, as with ``body: Item``.
    """
    api = BoltAPI()

    @api.post("/users")
    async def create_user(body: Annotated[UserRead, USER_META]) -> UserRead:
        return body

    @api.post("/dataclass")
    async def create_dataclass(body: Annotated[NewUser, msgspec.Meta(description="A new user.")]) -> dict:
        return {"type": type(body).__name__, "name": body.name}

    with TestClient(api) as client:
        created = client.post("/users", json={"id": 1, "name": "a"})
        assert created.status_code == 200
        assert created.json() == {"id": 1, "name": "a"}

        invalid = client.post("/users", json={"id": "x", "name": "a"})
        assert invalid.status_code == 422

        dataclass_body = client.post("/dataclass", json={"name": "b"})
        assert dataclass_body.status_code == 200
        assert dataclass_body.json() == {"type": "NewUser", "name": "b"}


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
