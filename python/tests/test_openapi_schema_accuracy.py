"""
Tests for OpenAPI schema accuracy improvements:
- Annotated constraints (ge/le/gt/lt/multiple_of, min_length/max_length/pattern)
- EnumType and Django TextChoices/IntegerChoices
- Literal type inference (string vs integer vs mixed)
- Struct field defaults in body schemas
"""

import dataclasses
import datetime
import enum
import sys
import types
from typing import Annotated, Any, Literal, NamedTuple, TypedDict

import msgspec
import pytest
from django.db import models
from openapi_spec_validator import validate

from django_bolt import BoltAPI
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication
from django_bolt.datastructures import UploadFile
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import ComponentNameCollisionError, SchemaGenerator
from django_bolt.openapi.spec import Components, SecurityScheme
from django_bolt.param_functions import File, Form, Query
from django_bolt.serializers import Serializer, computed_field, field
from django_bolt.serializers.types import Email, HttpsURL, PositiveInt
from django_bolt.testing import TestClient
from django_bolt.websocket import WebSocket


# Fixtures
class ConstrainedFilters(msgspec.Struct):
    page: Annotated[int, msgspec.Meta(ge=1)]
    size: Annotated[int, msgspec.Meta(ge=1, le=100)] = 20
    ratio: Annotated[float, msgspec.Meta(gt=0.0, lt=1.0)] | None = None
    step: Annotated[int, msgspec.Meta(multiple_of=5)] = 10


class StringConstrainedQuery(msgspec.Struct):
    name: Annotated[str, msgspec.Meta(min_length=1, max_length=50)]
    code: Annotated[str, msgspec.Meta(pattern=r"^[A-Z]{3}$")] | None = None


class ResponseWithDefaults(msgspec.Struct):
    message: str = "hello"
    count: int = 0
    active: bool = True


class NoDefaultsResponse(msgspec.Struct):
    id: str
    name: str


class RegularEnum(enum.StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class IntEnum(enum.Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class DjangoStatus(models.TextChoices):
    PLANNED = "planned", "Planned"
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"


class DjangoPriority(models.IntegerChoices):
    LOW = 1, "Low"
    MEDIUM = 2, "Medium"
    HIGH = 3, "High"


# Helpers
def _get_schema(api: BoltAPI) -> dict:
    """Helper to get OpenAPI schema dict from an API instance."""
    api._register_openapi_routes()
    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")
        assert response.status_code == 200
        return response.json()


def _get_param(params: list[dict], name: str) -> dict:
    """Find a parameter by name in the parameters list."""
    for p in params:
        if p["name"] == name:
            return p
    raise AssertionError(f"Parameter '{name}' not found in {[p['name'] for p in params]}")


def _get_query_param_schema(query_type: type, param_name: str) -> dict:
    """Build an API with a query struct and return one OpenAPI parameter schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def get_items(query: Annotated[query_type, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    return _get_param(params, param_name)["schema"]


def _get_response_component_schema(response_type: type, path: str = "/item") -> dict:
    """Build an API with a response struct and return its component schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get(path)
    async def get_item() -> response_type:
        pass

    schema = _get_schema(api)
    return schema["components"]["schemas"][response_type.__name__]


def test_int_ge_constraint():
    """Test that an annotated int with a ge constraint produces the correct schema."""
    page_schema = _get_query_param_schema(ConstrainedFilters, "page")
    assert page_schema["type"] == "integer"
    assert page_schema["minimum"] == 1
    assert "exclusiveMinimum" not in page_schema


def test_int_ge_le_constraints():
    """Test that an annotated int with a ge and le constraint produces the correct schema."""
    size_schema = _get_query_param_schema(ConstrainedFilters, "size")
    assert size_schema["minimum"] == 1
    assert size_schema["maximum"] == 100


def test_float_gt_lt_constraints():
    """Test that an annotated float with a gt and lt constraint produces the correct schema.

    `ratio` is `Annotated[float, ...] | None = None`. A query value cannot
    be null, so the parameter has the schema of the constrained float.
    """
    ratio_schema = _get_query_param_schema(ConstrainedFilters, "ratio")
    inner = ratio_schema
    assert inner["exclusiveMinimum"] == 0.0
    assert inner["exclusiveMaximum"] == 1.0
    assert "minimum" not in inner
    assert "maximum" not in inner


def test_int_multiple_of_constraint():
    """Test that an annotated int with a multiple_of constraint produces the correct schema."""
    step_schema = _get_query_param_schema(ConstrainedFilters, "step")
    assert step_schema["multipleOf"] == 5


def test_unconstrained_int_has_no_constraint_fields():
    """Test that an unconstrained int produces no constraint fields."""

    class SimpleQuery(msgspec.Struct):
        page: int = 1

    page_schema = _get_query_param_schema(SimpleQuery, "page")
    assert page_schema["type"] == "integer"
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        assert key not in page_schema, f"Unexpected constraint '{key}' on unconstrained int"


def test_str_min_max_length_constraints():
    """Test that an annotated str with a min_length and max_length constraint produces the correct schema."""
    name_schema = _get_query_param_schema(StringConstrainedQuery, "name")
    assert name_schema["type"] == "string"
    assert name_schema["minLength"] == 1
    assert name_schema["maxLength"] == 50


def test_str_pattern_constraint():
    """Test that an annotated str with a pattern constraint produces the correct schema.

    `code` is `Annotated[str, ...] | None = None`. A query value cannot
    be null, so the parameter has the schema of the constrained string.
    """
    code_schema = _get_query_param_schema(StringConstrainedQuery, "code")
    inner = code_schema
    assert inner["type"] == "string"
    assert inner["pattern"] == r"^[A-Z]{3}$"


def test_str_enum_produces_string_enum_schema():
    """Test that an annotated str with an enum constraint produces the correct schema.

    All four enum-as-nullable tests below use `Foo | None = None`. A query
    value cannot be null, so the parameter has the schema of the enum.
    """

    class FilterQuery(msgspec.Struct):
        status: RegularEnum | None = None

    status_schema = _get_query_param_schema(FilterQuery, "status")
    inner = status_schema
    assert inner["type"] == "string"
    assert set(inner["enum"]) == {"active", "inactive"}


def test_int_enum_produces_integer_enum_schema():
    """Test that an annotated int with an enum constraint produces the correct schema."""

    class FilterQuery(msgspec.Struct):
        priority: IntEnum | None = None

    priority_schema = _get_query_param_schema(FilterQuery, "priority")
    inner = priority_schema
    assert inner["type"] == "integer"
    assert set(inner["enum"]) == {1, 2, 3}


def test_django_text_choices_produces_string_enum():
    """Test that a Django TextChoices enum produces the correct schema."""

    class FilterQuery(msgspec.Struct):
        status: DjangoStatus | None = None

    status_schema = _get_query_param_schema(FilterQuery, "status")
    inner = status_schema
    assert inner["type"] == "string"
    assert set(inner["enum"]) == {"planned", "active", "completed"}


def test_django_integer_choices_produces_integer_enum():
    """Test that a Django IntegerChoices enum produces the correct schema."""

    class FilterQuery(msgspec.Struct):
        priority: DjangoPriority | None = None

    priority_schema = _get_query_param_schema(FilterQuery, "priority")
    inner = priority_schema
    assert inner["type"] == "integer"
    assert set(inner["enum"]) == {1, 2, 3}


def test_literal_string_query_param():
    """Test that a literal string query param produces the correct schema."""

    class SortQuery(msgspec.Struct):
        order: Literal["asc", "desc"] = "asc"

    order_schema = _get_query_param_schema(SortQuery, "order")
    assert order_schema["type"] == "string"
    assert set(order_schema["enum"]) == {"asc", "desc"}


def test_literal_integers_produces_integer_type():
    """Test that a literal integer query param produces the correct schema."""

    class PageQuery(msgspec.Struct):
        size: Literal[10, 25, 50, 100] = 10

    size_schema = _get_query_param_schema(PageQuery, "size")
    assert size_schema["type"] == "integer"
    assert set(size_schema["enum"]) == {10, 25, 50, 100}


def test_bare_mixed_literal_query_param_has_enum_without_type():
    """Test that a bare mixed-type Literal produces an enum schema with no inferred type."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def get_items(value: Literal["asc", 1] = "asc") -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    value = _get_param(params, "value")
    assert set(value["schema"]["enum"]) == {"asc", 1}
    assert "type" not in value["schema"]


def test_response_struct_fields_have_defaults():
    """Test that a struct field with a default produces the correct schema."""
    props = _get_response_component_schema(ResponseWithDefaults, path="/status")["properties"]
    assert props["message"]["default"] == "hello"
    assert props["count"]["default"] == 0
    assert props["active"]["default"] is True


def test_response_struct_required_fields_have_no_default():
    """Test that a required struct field with no default produces the correct schema."""
    no_defaults_schema = _get_response_component_schema(NoDefaultsResponse)
    props = no_defaults_schema["properties"]

    assert "default" not in props["id"]
    assert "default" not in props["name"]
    assert set(no_defaults_schema["required"]) == {"id", "name"}


def test_response_struct_reference_field_with_default_none():
    """Test that a reference field with a default of None produces the correct schema.

    Under OpenAPI 3.1 the nullability is expressed via `null` in the
    type union via `anyOf` rather than the legacy 3.0 `allOf` + ref
    workaround.
    """

    class Inner(msgspec.Struct):
        value: str

    class Outer(msgspec.Struct):
        name: str
        inner: Inner | None = None

    outer = _get_response_component_schema(Outer)
    assert outer["required"] == ["name"]
    inner = outer["properties"]["inner"]
    assert inner["default"] is None
    assert inner["anyOf"] == [
        {"$ref": "#/components/schemas/Inner"},
        {"type": "null"},
    ]


# ---------- operations: responses, request body, parameters ----------


class Widget(msgspec.Struct):
    name: str


class Problem(msgspec.Struct):
    detail: str


class WidgetFilters(msgspec.Struct):
    q: str
    page: int = 1


@dataclasses.dataclass
class Warehouse:
    city: str


class Size(TypedDict):
    width: int


class Position(NamedTuple):
    x: int


class Shelf(msgspec.Struct):
    pair: tuple[Widget, Problem]
    many: tuple[Widget, ...]
    by_name: dict[str, Widget]
    noted: Annotated[Widget, msgspec.Meta(description="The first widget")]
    widgets: list[Widget]
    position: Position


class Circle(msgspec.Struct, tag="circle"):
    center: Widget
    labels: dict[str, str]
    note: str | None
    radius: float = 1.0


class Square(msgspec.Struct, tag="square"):
    side: float


WIDGET_REF = {"$ref": "#/components/schemas/Widget"}
PROBLEM_REF = {"$ref": "#/components/schemas/Problem"}


def _json_content(schema: dict) -> dict:
    return {"application/json": {"schema": schema}}


def test_response_model_dict_documents_each_status_and_the_default_response():
    """Each status of a ``response_model`` dict gets a response. The ``...`` entry is the ``default`` response."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/typed", response_model={200: Widget, 204: None, 299: Problem, ...: Problem})
    async def typed():
        pass

    @api.get("/bare", response_model={200: Widget, ...: None})
    async def bare():
        pass

    schema = _get_schema(api)
    validate(schema)

    assert schema["paths"]["/typed"]["get"]["responses"] == {
        "200": {"description": "OK", "content": _json_content(WIDGET_REF)},
        "204": {"description": "No Content"},
        "299": {"description": "Response 299", "content": _json_content(PROBLEM_REF)},
        "default": {"description": "Default response", "content": _json_content(PROBLEM_REF)},
    }
    assert schema["paths"]["/bare"]["get"]["responses"]["default"] == {"description": "Default response"}


def test_tagged_union_examples_show_nested_values_for_each_response():
    """An example holds a value for a nested struct, a dict and an optional field. The ``...`` entry has examples."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/shape", response_model={200: Circle | Square, ...: Circle | Square})
    async def shape():
        pass

    responses = _get_schema(api)["paths"]["/shape"]["get"]["responses"]
    examples = {
        "circle": {
            "summary": "Circle variant",
            "value": {"type": "circle", "center": {"name": "string"}, "labels": {}, "note": "string", "radius": 0.0},
        },
        "square": {"summary": "Square variant", "value": {"type": "square", "side": 0.0}},
    }

    assert responses["200"]["content"]["application/json"]["examples"] == examples
    assert responses["default"]["content"]["application/json"]["examples"] == examples


def test_request_body_is_required_when_the_route_needs_a_value():
    """A JSON body is required. A form body is required when one field has no default. Both show a 422."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.post("/json")
    async def json_body(widget: Widget):
        pass

    @api.post("/form")
    async def form_body(
        title: Annotated[str, Form()],
        note: Annotated[str | None, Form()],
        tag: Annotated[str, Form()] = "x",
    ):
        pass

    @api.post("/optional-form")
    async def optional_form_body(tag: Annotated[str, Form()] = "x"):
        pass

    @api.post("/file")
    async def file_body(file: Annotated[UploadFile, File()]):
        pass

    schema = _get_schema(api)
    validate(schema)
    operations = {path: item["post"] for path, item in schema["paths"].items()}

    assert operations["/json"]["requestBody"]["required"] is True
    assert operations["/form"]["requestBody"]["required"] is True
    assert operations["/form"]["requestBody"]["content"]["multipart/form-data"]["schema"] == {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "note": {"type": "string"},
            "tag": {"type": "string", "default": "x"},
        },
        "required": ["title"],
    }
    assert operations["/optional-form"]["requestBody"]["required"] is False
    assert operations["/file"]["requestBody"]["required"] is True
    assert all("422" in operation["responses"] for operation in operations.values())


def test_parameters_list_each_struct_field_and_each_path_parameter_of_the_url():
    """A query struct gives one parameter for each field. A URL parameter that the handler does not take is a string."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/widgets/{widget_id}/parts/{part}")
    async def get_part(request, widget_id: int, filters: Annotated[WidgetFilters, Query()], limit: int = 5):
        pass

    schema = _get_schema(api)
    validate(schema)
    parameters = schema["paths"]["/widgets/{widget_id}/parts/{part}"]["get"]["parameters"]

    assert [(p["name"], p["in"], p["required"], p["schema"], p["description"]) for p in parameters] == [
        ("widget_id", "path", True, {"type": "integer"}, "Parameter widget_id"),
        ("q", "query", True, {"type": "string"}, "Parameter q"),
        ("page", "query", False, {"type": "integer", "default": 1}, "Parameter page"),
        ("limit", "query", False, {"type": "integer", "default": 5}, "Parameter limit"),
        ("part", "path", True, {"type": "string"}, "Parameter part"),
    ]


@pytest.mark.parametrize(
    ("module_name", "expected_tags"),
    [("shop.orders", ["Orders"]), ("shop.users.api", ["Users"]), ("api", None)],
)
def test_operation_tag_comes_from_the_module_of_the_handler(monkeypatch, module_name, expected_tags):
    """The tag is the last part of the module name. A module with the name ``api`` gives the part before it."""
    monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    async def handler():
        pass

    handler.__module__ = module_name
    api.get("/thing")(handler)

    schema = _get_schema(api)

    assert schema["paths"]["/thing"]["get"].get("tags") == expected_tags
    assert schema.get("tags") == ([{"name": expected_tags[0]}] if expected_tags else None)


def test_security_schemes_list_only_the_backends_that_the_routes_use():
    """The document has one scheme for each backend in use. A scheme from the config stays as it is."""
    custom = SecurityScheme(type="apiKey", name="X-Custom", security_scheme_in="query")
    config = OpenAPIConfig(
        title="Test API", version="1.0.0", components=Components(security_schemes={"ApiKeyAuth": custom})
    )
    with_custom = BoltAPI(openapi_config=config)
    plain = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    for api in (with_custom, plain):

        @api.get("/key", auth=[APIKeyAuthentication(api_keys={"k"}, header="X-Token")], guards=[IsAuthenticated()])
        async def by_key():
            pass

    @with_custom.websocket("/ws/{room}", auth=[JWTAuthentication(secret="s" * 32)], guards=[IsAuthenticated()])
    async def chat(websocket: WebSocket, room: str):
        """Chat room."""

    custom_schema = _get_schema(with_custom)
    plain_schema = _get_schema(plain)
    validate(custom_schema)
    validate(plain_schema)

    assert plain_schema["components"]["securitySchemes"] == {
        "ApiKeyAuth": {"type": "apiKey", "name": "X-Token", "in": "header"}
    }
    assert custom_schema["components"]["securitySchemes"] == {
        "ApiKeyAuth": {"type": "apiKey", "name": "X-Custom", "in": "query"},
        "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
    }
    # A WebSocket operation shows its backend, its docstring and its path parameter.
    websocket_operation = custom_schema["paths"]["/ws/{room}"]["get"]
    assert websocket_operation["security"] == [{"BearerAuth": []}]
    assert websocket_operation["summary"] == "WebSocket: Chat room."
    assert [p["name"] for p in websocket_operation["parameters"]] == ["room", "Upgrade", "Connection"]
    assert all(p["required"] for p in websocket_operation["parameters"])
    assert websocket_operation["responses"]["101"]["headers"] == {
        "Upgrade": {"schema": {"type": "string", "enum": ["websocket"]}, "required": False, "deprecated": False},
        "Connection": {"schema": {"type": "string", "enum": ["Upgrade"]}, "required": False, "deprecated": False},
    }


@pytest.mark.parametrize(
    ("response_type", "expected"),
    [
        (dict[str, Widget], {"type": "object", "additionalProperties": WIDGET_REF}),
        (tuple[Widget, ...], {"type": "array", "items": WIDGET_REF}),
        (
            tuple[Widget, Problem],
            {"type": "array", "prefixItems": [WIDGET_REF, PROBLEM_REF], "minItems": 2, "maxItems": 2},
        ),
        (
            frozenset[Position],
            {"type": "array", "items": {"$ref": "#/components/schemas/Position"}, "uniqueItems": True},
        ),
        (Warehouse, {"$ref": "#/components/schemas/Warehouse"}),
        (Size, {"$ref": "#/components/schemas/Size"}),
        (Position, {"$ref": "#/components/schemas/Position"}),
        (object, {"type": "object"}),
    ],
    ids=["dict", "tuple", "fixed-tuple", "frozenset", "dataclass", "typeddict", "namedtuple", "unknown"],
)
def test_response_type_refers_to_the_component_of_each_named_type(response_type, expected):
    """A named type is a component reference, also inside a collection. An unknown type is an object."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/thing")
    async def thing() -> response_type:
        pass

    schema = _get_schema(api)
    validate(schema)

    assert schema["paths"]["/thing"]["get"]["responses"]["200"]["content"] == _json_content(expected)


def test_struct_field_refers_to_the_component_of_each_named_type():
    """A struct field with a named type inside a collection is a component reference."""
    properties = _get_response_component_schema(Shelf)["properties"]

    assert properties == {
        "pair": {"type": "array", "prefixItems": [WIDGET_REF, PROBLEM_REF], "minItems": 2, "maxItems": 2},
        "many": {"type": "array", "items": WIDGET_REF},
        "by_name": {"type": "object", "additionalProperties": WIDGET_REF},
        "noted": WIDGET_REF,
        "widgets": {"type": "array", "items": WIDGET_REF},
        "position": {"$ref": "#/components/schemas/Position"},
    }


# ---------- title / description on component schemas -----------------
#
# `_struct_to_schema` carries the struct's `__name__` and `__doc__`
# through to the OpenAPI Schema so that downstream codegen (notably
# `openapi-typescript`) renders type labels and JSDoc on the generated
# types. Mirrors the shape `msgspec.json.schema_components` produces.


def test_component_schema_carries_title_from_struct_name():
    """The component schema's `title` field equals the struct's class name."""

    class TitledStruct(msgspec.Struct):
        name: str

    schema = _get_response_component_schema(TitledStruct)
    assert schema["title"] == "TitledStruct"


def test_component_schema_carries_description_from_docstring():
    """The component schema's `description` field equals the struct's docstring."""

    class DocumentedStruct(msgspec.Struct):
        """Concise summary of the struct."""

        name: str

    schema = _get_response_component_schema(DocumentedStruct)
    assert schema["description"] == "Concise summary of the struct."


def test_component_schema_strips_docstring_indentation():
    """Multi-line docstrings have uniform leading indentation removed
    (the same `inspect.cleandoc` behavior `msgspec.json.schema_components`
    applies), so the rendered JSDoc isn't wrapped in stray spaces."""

    class MultiLineStruct(msgspec.Struct):
        """Summary line.

        Continuation paragraph that explains the struct in more detail.
        """

        name: str

    schema = _get_response_component_schema(MultiLineStruct)
    assert schema["description"] == ("Summary line.\n\nContinuation paragraph that explains the struct in more detail.")


def test_component_schema_omits_description_when_no_docstring():
    """Structs without a docstring don't carry an empty `description` —
    the field is dropped from the emitted JSON entirely."""

    class BareStruct(msgspec.Struct):
        name: str

    schema = _get_response_component_schema(BareStruct)
    assert "description" not in schema
    # title is unconditional
    assert schema["title"] == "BareStruct"


# ---------- documented constrained types (custom types) --------------
#
# Regression tests for #235. msgspec.inspect wraps a field whose
# `Annotated[T, Meta(...)]` carries informational fields (description /
# examples / title) in a `Metadata` node — distinct from a constraints-only
# Meta, which stays a bare `*Type`. The generator must unwrap that node to
# the underlying type (+ constraints + docs); otherwise the field falls
# through to the generic `object` fallback and codegen tools (typescript-fetch,
# openapi-typescript) emit `object` instead of string/integer. Every built-in
# type in `serializers.types` (Email, PositiveInt, HttpsURL, …) carries a
# `description`, so all of them hit this path.


def test_documented_str_constraint_renders_as_string_not_object():
    """A str field with constraints AND a description must render as `string`."""

    class DocStr(msgspec.Struct):
        code: Annotated[str, msgspec.Meta(max_length=10, pattern=r"^[A-Z]+$", description="A code")]

    props = _get_response_component_schema(DocStr)["properties"]
    # Without the fix this is {"type": "object"} (the reported bug).
    assert props["code"]["type"] == "string"
    assert props["code"]["maxLength"] == 10
    assert props["code"]["pattern"] == r"^[A-Z]+$"
    assert props["code"]["description"] == "A code"


def test_documented_int_constraint_renders_as_integer_not_object():
    """An int field with a constraint AND a description must render as `integer`."""

    class DocInt(msgspec.Struct):
        qty: Annotated[int, msgspec.Meta(gt=0, description="Positive quantity")]

    props = _get_response_component_schema(DocInt)["properties"]
    assert props["qty"]["type"] == "integer"
    assert props["qty"]["exclusiveMinimum"] == 0
    assert props["qty"]["description"] == "Positive quantity"


def test_documented_type_carries_examples():
    """The `examples` carried by a documented Meta survive onto the schema."""

    class WithExamples(msgspec.Struct):
        name: Annotated[str, msgspec.Meta(min_length=1, examples=["alice", "bob"])]

    props = _get_response_component_schema(WithExamples)["properties"]
    assert props["name"]["type"] == "string"
    assert props["name"]["minLength"] == 1
    assert props["name"]["examples"] == ["alice", "bob"]


def test_extra_json_schema_keys_in_camel_case_reach_the_schema():
    """A JSON Schema keyword of ``Meta(extra_json_schema=...)`` keeps its camelCase name in the document."""

    class Upload(msgspec.Struct):
        payload: Annotated[
            str,
            msgspec.Meta(
                extra_json_schema={
                    "contentEncoding": "base64",
                    "readOnly": True,
                    "externalDocs": {"url": "https://example.com/payload"},
                }
            ),
        ]

    assert _get_response_component_schema(Upload)["properties"]["payload"] == {
        "type": "string",
        "contentEncoding": "base64",
        "readOnly": True,
        "externalDocs": {"url": "https://example.com/payload"},
    }


def test_builtin_custom_types_in_serializer_render_concrete_types():
    """The reported scenario: a Serializer using built-in Email/PositiveInt/HttpsURL.

    Each property must carry its concrete base type, not the generic `object`.
    """

    class Account(Serializer):
        email: Email
        age: PositiveInt
        site: HttpsURL

    props = _get_response_component_schema(Account)["properties"]
    assert props["email"]["type"] == "string"
    assert props["email"]["maxLength"] == 254
    assert props["age"]["type"] == "integer"
    assert props["age"]["exclusiveMinimum"] == 0
    assert props["site"]["type"] == "string"
    # None of them may collapse to a bare object.
    assert all(props[f]["type"] != "object" for f in ("email", "age", "site"))


def test_read_only_field_is_not_in_the_request_body_schema():
    """Input cannot set a read_only field. So only the response schema has it, as readOnly."""

    class Owner(Serializer):
        id: int = field(read_only=True, default=0)
        name: str

    class Pet(Serializer):
        id: int = field(read_only=True, default=0)
        name: str
        created: str | None = None
        owner: Owner | None = None

        class Config:
            read_only = {"created"}

    class Plain(Serializer):
        name: str

    class BodyOnly(Serializer):
        id: int = field(read_only=True, default=0)
        name: str

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.post("/pets")
    async def create_pet(body: Pet) -> Pet:
        pass

    @api.post("/plain")
    async def create_plain(body: Plain) -> Plain:
        pass

    @api.post("/owners")
    async def create_owner(body: BodyOnly) -> dict:
        pass

    spec = _get_schema(api)
    validate(spec)
    schemas = spec["components"]["schemas"]
    operation = spec["paths"]["/pets"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PetInput"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Pet"
    }

    assert schemas["PetInput"]["title"] == "Pet"
    assert list(schemas["PetInput"]["properties"]) == ["name", "owner"]
    assert schemas["PetInput"]["required"] == ["name"]
    # The nested serializer of a request body is an input schema too.
    assert schemas["PetInput"]["properties"]["owner"]["anyOf"][0] == {"$ref": "#/components/schemas/OwnerInput"}
    assert list(schemas["OwnerInput"]["properties"]) == ["name"]

    assert schemas["Pet"]["properties"]["id"] == {"type": "integer", "default": 0, "readOnly": True}
    assert schemas["Pet"]["properties"]["created"]["readOnly"] is True
    assert "readOnly" not in schemas["Pet"]["properties"]["name"]
    assert schemas["Pet"]["properties"]["owner"]["anyOf"][0] == {"$ref": "#/components/schemas/Owner"}
    assert schemas["Owner"]["properties"]["id"]["readOnly"] is True

    # A serializer that is only a request body keeps its name.
    assert spec["paths"]["/owners"]["post"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BodyOnly"
    }
    assert list(schemas["BodyOnly"]["properties"]) == ["name"]
    assert "BodyOnlyInput" not in schemas

    # A serializer with no read_only field has one schema.
    plain = spec["paths"]["/plain"]["post"]
    assert plain["requestBody"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Plain"}
    assert "PlainInput" not in schemas


class ComputedOwner(Serializer):
    name: str


class ComputedUser(Serializer):
    """A user."""

    first_name: str
    last_name: str
    password: str = field(write_only=True, default="")
    internal: str = field(exclude=True, default="")

    @computed_field(description="Name for display", title="Display name", deprecated=True, examples=["A B"])
    def full_name(self) -> str:
        """The docstring that the description argument replaces."""
        return f"{self.first_name} {self.last_name}"

    @computed_field
    def initials(self) -> str:
        """First letters of the names.

        In upper case.
        """
        return (self.first_name[:1] + self.last_name[:1]).upper()

    @computed_field(alias="ownerInfo")
    def owner(self) -> ComputedOwner | None:
        return None

    @computed_field
    def scores(self) -> list[int]:
        return [1]

    @computed_field(include_in_schema=False)
    def debug(self) -> str:
        return "d"


def test_computed_fields_are_in_the_response_schema_only():
    """The response schema has each computed field, read-only and required. The request body schema has none."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.post("/users")
    async def create_user(body: ComputedUser) -> ComputedUser:
        return body

    spec = _get_schema(api)
    validate(spec)
    schemas = spec["components"]["schemas"]
    operation = spec["paths"]["/users"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ComputedUserInput"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ComputedUser"
    }

    response_schema = schemas["ComputedUser"]
    props = response_schema["properties"]
    # A write_only field and a field with exclude=True are not in the output, so not in its schema.
    assert list(props) == ["first_name", "last_name", "full_name", "initials", "ownerInfo", "scores"]
    assert response_schema["required"] == ["first_name", "last_name", "full_name", "initials", "ownerInfo", "scores"]
    assert props["full_name"] == {
        "type": "string",
        "readOnly": True,
        "description": "Name for display",
        "title": "Display name",
        "deprecated": True,
        "examples": ["A B"],
    }
    assert props["initials"] == {
        "type": "string",
        "readOnly": True,
        "description": "First letters of the names.\n\nIn upper case.",
    }
    assert props["ownerInfo"] == {
        "anyOf": [{"$ref": "#/components/schemas/ComputedOwner"}, {"type": "null"}],
        "readOnly": True,
    }
    assert props["scores"] == {"type": "array", "items": {"type": "integer"}, "readOnly": True}

    request_schema = schemas["ComputedUserInput"]
    assert list(request_schema["properties"]) == ["first_name", "last_name", "password", "internal"]
    assert request_schema["required"] == ["first_name", "last_name"]
    assert request_schema["properties"]["password"] == {"type": "string", "default": "", "writeOnly": True}
    assert request_schema["description"] == "A user."

    # The schema of the response is the shape of the response.
    with TestClient(api) as client:
        response = client.post("/users", json={"first_name": "a", "last_name": "b", "password": "p", "internal": "i"})
        assert response.status_code == 200
        body = response.json()
        assert list(body) == [*props, "debug"]
        assert body["debug"] == "d"


def test_documented_constrained_field_matches_msgspec_schema():
    """Bolt's component schema for documented custom types matches msgspec's own.

    This is the contract codegen tools rely on: the spec Bolt emits must be the
    same one `msgspec.json.schema` would produce for the equivalent struct.
    """

    class Account(Serializer):
        email: Email
        age: PositiveInt
        site: HttpsURL

    bolt_props = _get_response_component_schema(Account)["properties"]
    msgspec_props = msgspec.json.schema(Account)["$defs"]["Account"]["properties"]
    for field_name in ("email", "age", "site"):
        assert bolt_props[field_name]["type"] == msgspec_props[field_name]["type"]


def test_field_options_are_in_the_serializer_schema():
    """description, title, examples and deprecated of field() are in the property schema."""

    class Maker(Serializer):
        name: str = field(description="Name of the maker")

    class Product(Serializer):
        sku: str = field(description="Stock unit", title="SKU", examples=["AB-1", "AB-2"], deprecated=True)
        price: float = field(default=1.5, description="Price in euro")
        maker: Maker = field(description="Who makes it", title="Maker of the product", deprecated=True)
        second_maker: Maker | None = field(default=None, description="Optional maker")
        makers: list[Maker] = field(default_factory=list, description="Each maker")
        note: Annotated[str, msgspec.Meta(description="From Meta")] = field(default="", description="From field()")
        plain: int = 0

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.post("/products")
    async def create_product(body: Product) -> Product:
        pass

    spec = _get_schema(api)
    validate(spec)
    operation = spec["paths"]["/products"]["post"]
    ref = {"$ref": "#/components/schemas/Product"}
    # The request body and the response use the same schema.
    assert operation["requestBody"]["content"]["application/json"]["schema"] == ref
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == ref

    props = spec["components"]["schemas"]["Product"]["properties"]
    assert props["sku"] == {
        "type": "string",
        "description": "Stock unit",
        "title": "SKU",
        "examples": ["AB-1", "AB-2"],
        "deprecated": True,
    }
    assert props["price"] == {"type": "number", "default": 1.5, "description": "Price in euro"}
    # A reference to a component keeps the options next to it.
    assert props["maker"] == {
        "allOf": [{"$ref": "#/components/schemas/Maker"}],
        "description": "Who makes it",
        "title": "Maker of the product",
        "deprecated": True,
    }
    assert props["second_maker"]["description"] == "Optional maker"
    assert props["second_maker"]["anyOf"][0] == {"$ref": "#/components/schemas/Maker"}
    assert props["makers"]["description"] == "Each maker"
    assert props["makers"]["items"] == {"$ref": "#/components/schemas/Maker"}
    # field() wins over msgspec.Meta.
    assert props["note"]["description"] == "From field()"
    assert props["plain"] == {"type": "integer", "default": 0}
    # The nested serializer has the options of its own fields.
    assert spec["components"]["schemas"]["Maker"]["properties"]["name"] == {
        "type": "string",
        "description": "Name of the maker",
    }
    assert spec["components"]["schemas"]["Product"]["required"] == ["sku", "maker"]


class HiddenFieldOwner(Serializer):
    name: str


class HiddenFieldItem(Serializer):
    name: str
    token: str = field(include_in_schema=False)
    trace: str = field(include_in_schema=False, default="t")
    owner: HiddenFieldOwner | None = field(include_in_schema=False, default=None)


def test_field_with_include_in_schema_false_is_not_in_the_schema():
    """The schema has no such property. The field still validates and serializes."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.post("/items")
    async def create_item(body: HiddenFieldItem) -> HiddenFieldItem:
        return body

    spec = _get_schema(api)
    validate(spec)
    component = spec["components"]["schemas"]["HiddenFieldItem"]
    assert list(component["properties"]) == ["name"]
    assert component["required"] == ["name"]
    # No component for a type that only a hidden field uses.
    assert "HiddenFieldOwner" not in spec["components"]["schemas"]

    with TestClient(api) as client:
        response = client.post("/items", json={"name": "n", "token": "k", "owner": {"name": "o"}})
        assert response.status_code == 200
        assert response.json() == {"name": "n", "token": "k", "trace": "t", "owner": {"name": "o"}}
        # The hidden field is still required.
        assert client.post("/items", json={"name": "n"}).status_code == 422


class RequiredFieldFilters(Serializer):
    code: str = field(description="Short code", examples=["AB"])
    secret: str = field(include_in_schema=False)


def test_required_field_options_are_in_query_and_form_schemas():
    """A required field of a query or form Serializer has its field() options in the schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def get_items(query: Annotated[RequiredFieldFilters, Query()]) -> dict:
        pass

    @api.post("/items")
    async def post_items(form: Annotated[RequiredFieldFilters, Form()]) -> dict:
        pass

    spec = _get_schema(api)
    validate(spec)
    params = spec["paths"]["/items"]["get"]["parameters"]
    assert [(param["name"], param["required"]) for param in params] == [("code", True)]
    assert params[0]["schema"] == {"type": "string", "description": "Short code", "examples": ["AB"]}

    content = spec["paths"]["/items"]["post"]["requestBody"]["content"]
    for media_type in content.values():
        assert media_type["schema"]["properties"] == {
            "code": {"type": "string", "description": "Short code", "examples": ["AB"]}
        }
        assert media_type["schema"]["required"] == ["code"]


def test_documented_query_struct_field_renders_concrete_type():
    """A documented constrained field on a query struct types the parameter.

    Exercises the `_extract_parameters` -> `_msgspec_field_schema` path, which is
    distinct from the response-component path.
    """

    class Filters(msgspec.Struct):
        code: Annotated[str, msgspec.Meta(max_length=5, description="Short code")] = "AB"

    code_schema = _get_query_param_schema(Filters, "code")
    assert code_schema["type"] == "string"
    assert code_schema["maxLength"] == 5
    assert code_schema["description"] == "Short code"


def test_documented_custom_type_as_bare_response_model():
    """A documented custom type used directly as a (nested) response model.

    `-> list[Email]` reaches the generator as a raw `typing.Annotated` still
    carrying its msgspec Meta; the array items must type as `string`.
    """
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/emails")
    async def list_emails() -> list[Email]:
        pass

    schema = _get_schema(api)
    items = schema["paths"]["/emails"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["items"]
    assert items["type"] == "string"
    assert items["maxLength"] == 254


def test_nullable_documented_custom_type_unwraps_inside_union():
    """`HttpsURL | None` — the Metadata node nested in the union must unwrap.

    Nullable custom types are the common shape (`website: HttpsURL | None = None`);
    under OpenAPI 3.1 the type sits in the first `anyOf` arm beside `{"type": "null"}`.
    """

    class Profile(Serializer):
        website: HttpsURL | None = None

    props = _get_response_component_schema(Profile)["properties"]
    arms = props["website"]["anyOf"]
    typed = next(arm for arm in arms if arm.get("type") != "null")
    assert typed["type"] == "string"
    assert typed["maxLength"] == 200
    assert {"type": "null"} in arms


def test_constraints_only_meta_still_omits_description():
    """A constraints-only Meta (no docs) stays a bare *Type and gains no description.

    Guards the boundary the fix hinges on: only Meta with informational fields
    becomes a Metadata node. This must keep working unchanged.
    """

    class ConstraintsOnly(msgspec.Struct):
        n: Annotated[int, msgspec.Meta(ge=1, le=9)]

    props = _get_response_component_schema(ConstraintsOnly)["properties"]
    assert props["n"]["type"] == "integer"
    assert props["n"]["minimum"] == 1
    assert props["n"]["maximum"] == 9
    assert "description" not in props["n"]


# ---------- typed dict value types (dict[K, V]) ----------------------
#
# Regression tests for the dict value-type erasure bug. Both dict code paths in
# `_type_to_schema` (the msgspec `DictType` branch for Struct fields, and the
# typing `origin is dict` branch for bare/nested response models) used to drop
# V and emit `additionalProperties: true` unconditionally. Mirroring the list
# handlers, they now recurse into V so `dict[str, V]` emits
# `additionalProperties: <schema for V>`, matching `msgspec.json.schema`.
# Untyped values (bare dict / dict[str, Any]) must still emit
# `additionalProperties: true` rather than regressing to `{"type": "object"}`.


def test_dict_str_int_value_type_preserved():
    """`dict[str, int]` must carry the value type, not collapse to `true`."""

    class IntMap(msgspec.Struct):
        counts: dict[str, int]

    props = _get_response_component_schema(IntMap)["properties"]
    assert props["counts"]["type"] == "object"
    # Without the fix this is `additionalProperties: true` (the bug).
    assert props["counts"]["additionalProperties"] == {"type": "integer"}


def test_dict_str_str_value_type_preserved():
    """`dict[str, str]` renders a string-valued `additionalProperties`."""

    class StrMap(msgspec.Struct):
        labels: dict[str, str]

    props = _get_response_component_schema(StrMap)["properties"]
    assert props["labels"]["additionalProperties"] == {"type": "string"}


def test_dict_str_struct_value_emits_ref_and_registers_component():
    """`dict[str, SomeStruct]` emits a `$ref` value and registers the component."""

    class DictValueItem(msgspec.Struct):
        x: int

    class StructMap(msgspec.Struct):
        items: dict[str, DictValueItem]

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/structmap")
    async def get_structmap() -> StructMap:
        pass

    components = _get_schema(api)["components"]["schemas"]
    ap = components["StructMap"]["properties"]["items"]["additionalProperties"]
    assert ap == {"$ref": "#/components/schemas/DictValueItem"}
    # The nested value struct must be registered as its own component.
    assert "DictValueItem" in components


def test_dict_str_optional_value_renders_anyof_with_null():
    """`dict[str, int | None]` renders an `anyOf` value with a `null` arm."""

    class OptMap(msgspec.Struct):
        maybe: dict[str, int | None]

    props = _get_response_component_schema(OptMap)["properties"]
    assert props["maybe"]["additionalProperties"]["anyOf"] == [
        {"type": "integer"},
        {"type": "null"},
    ]


class MaybeItem(msgspec.Struct):
    id: int


def test_optional_type_keeps_its_null_arm_outside_a_struct():
    """`T | None` as a response type, a body type or an item type says that null is valid."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/maybe")
    async def maybe() -> int | None:
        pass

    @api.get("/maybe-item")
    async def maybe_item() -> MaybeItem | None:
        pass

    @api.get("/maybe-in-list")
    async def maybe_in_list() -> list[int | None]:
        pass

    @api.post("/maybe-body")
    async def maybe_body(item: MaybeItem | None = None, page: int | None = None) -> dict[str, int | None]:
        pass

    paths = _get_schema(api)["paths"]

    def response_schema(path: str, method: str = "get") -> dict:
        return paths[path][method]["responses"]["200"]["content"]["application/json"]["schema"]

    nullable_int = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    nullable_item = {"anyOf": [{"$ref": "#/components/schemas/MaybeItem"}, {"type": "null"}]}
    assert response_schema("/maybe") == nullable_int == msgspec.json.schema(int | None)
    assert response_schema("/maybe-item") == nullable_item
    assert response_schema("/maybe-in-list") == {"type": "array", "items": nullable_int}
    assert response_schema("/maybe-body", "post") == {"type": "object", "additionalProperties": nullable_int}

    # Bolt decodes the JSON body `null` to None for this parameter.
    operation = paths["/maybe-body"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == nullable_item
    # A query parameter cannot carry null: `required: false` says that it is optional.
    (page,) = operation["parameters"]
    assert (page["schema"], page["required"]) == ({"type": "integer"}, False)


class OptionalFields(msgspec.Struct):
    name: str
    opt: str | None = None
    count: Annotated[int, msgspec.Meta(ge=1)] | None = None
    tags: list[str] | None = None
    label: str | None = "x"
    either: int | str | None = None


def test_an_optional_field_of_a_parameter_model_has_the_schema_of_a_parameter():
    """A query or form value cannot be null. A model field follows the rule of a parameter with no model."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/query-model")
    async def query_model(params: Annotated[OptionalFields, Query()]):
        pass

    @api.post("/form-model")
    async def form_model(params: Annotated[OptionalFields, Form()]):
        pass

    @api.get("/query")
    async def query(
        name: Annotated[str, Query()],
        opt: Annotated[str | None, Query()] = None,
        count: Annotated[Annotated[int, msgspec.Meta(ge=1)] | None, Query()] = None,
        tags: Annotated[list[str] | None, Query()] = None,
        label: Annotated[str | None, Query()] = "x",
    ):
        pass

    @api.post("/form")
    async def form(
        name: Annotated[str, Form()],
        opt: Annotated[str | None, Form()] = None,
        count: Annotated[Annotated[int, msgspec.Meta(ge=1)] | None, Form()] = None,
        tags: Annotated[list[str] | None, Form()] = None,
        label: Annotated[str | None, Form()] = "x",
    ):
        pass

    spec = _get_schema(api)
    validate(spec)
    paths = spec["paths"]

    def parameters(path: str) -> dict:
        return {p["name"]: (p["required"], p["schema"]) for p in paths[path]["get"]["parameters"]}

    def form_schema(path: str) -> dict:
        return paths[path]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]

    fields = {
        "name": {"type": "string"},
        "opt": {"type": "string"},
        "count": {"type": "integer", "minimum": 1},
        "tags": {"type": "array", "items": {"type": "string"}},
        "label": {"type": "string", "default": "x"},
    }
    # A union of two types or more keeps each arm. The null arm makes the None default valid.
    either = {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}], "default": None}

    model_parameters = parameters("/query-model")
    assert model_parameters.pop("either") == (False, either)
    assert model_parameters == {name: (name == "name", schema) for name, schema in fields.items()}
    assert model_parameters == parameters("/query")

    model_form = form_schema("/form-model")
    assert model_form["properties"].pop("either") == either
    assert model_form["properties"] == fields
    assert model_form == form_schema("/form")


def test_dict_str_any_keeps_additional_properties_true():
    """`dict[str, Any]` has no value type to describe — keep `true`, don't regress.

    Guards the boundary the fix hinges on: an untyped value (msgspec models it as
    `AnyType`) must NOT become `additionalProperties: {"type": "object"}`.
    """

    class AnyMap(msgspec.Struct):
        meta: dict[str, Any]

    props = _get_response_component_schema(AnyMap)["properties"]
    assert props["meta"]["type"] == "object"
    assert props["meta"]["additionalProperties"] is True


def test_dict_value_types_match_msgspec_schema():
    """Bolt's `additionalProperties` matches `msgspec.json.schema` for typed dicts.

    Compares the directly-comparable cases (primitive + nullable values); the
    struct-valued case differs only in `$ref` path (`#/components/schemas` vs
    msgspec's `#/$defs`) and is covered separately above.
    """

    class Maps(msgspec.Struct):
        counts: dict[str, int]
        labels: dict[str, str]
        maybe: dict[str, int | None]

    bolt_props = _get_response_component_schema(Maps)["properties"]
    msgspec_props = msgspec.json.schema_components((Maps,))[1]["Maps"]["properties"]
    for field_name in ("counts", "labels", "maybe"):
        assert bolt_props[field_name]["additionalProperties"] == msgspec_props[field_name]["additionalProperties"]


def test_dict_value_type_preserved_in_typing_path():
    """`-> dict[str, int]` exercises the typing `origin is dict` branch (not msgspec)."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/counts")
    async def get_counts() -> dict[str, int]:
        pass

    schema = _get_schema(api)
    resp = schema["paths"]["/counts"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert resp["type"] == "object"
    assert resp["additionalProperties"] == {"type": "integer"}


def test_untyped_dict_typing_path_keeps_additional_properties_true():
    """`-> dict[str, Any]` on the typing path keeps `true`, doesn't regress."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/raw")
    async def get_raw() -> dict[str, Any]:
        pass

    schema = _get_schema(api)
    resp = schema["paths"]["/raw"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert resp["type"] == "object"
    assert resp["additionalProperties"] is True


# ---------- factory defaults (default_factory) -----------------------
#
# Regression tests for #245. `_msgspec_field_schema` carried `field.default`
# onto the schema but never read `field.default_factory`, so factory-defaulted
# fields (the usual mutable-default case: `list`/`dict`) lost their `default`.
# It now materializes the factory, matching `msgspec.json.schema`.


class FactoryDefaults(msgspec.Struct):
    name: str = ""
    tags: list[str] = msgspec.field(default_factory=list)
    meta: dict[str, str] = msgspec.field(default_factory=dict)


def test_factory_default_list_materialized():
    """A `default_factory=list` field carries `default: []`, not no default."""
    props = _get_response_component_schema(FactoryDefaults, path="/factory")["properties"]
    # Without the fix `tags` has no `default` at all (the reported bug).
    assert props["tags"]["default"] == []


def test_factory_default_dict_materialized():
    """A `default_factory=dict` field carries `default: {}`."""
    props = _get_response_component_schema(FactoryDefaults, path="/factory")["properties"]
    assert props["meta"]["default"] == {}


def test_scalar_default_still_present_alongside_factory_defaults():
    """The pre-existing scalar-default path is untouched by the factory fallback."""
    props = _get_response_component_schema(FactoryDefaults, path="/factory")["properties"]
    assert props["name"]["default"] == ""


def test_factory_defaulted_fields_are_not_required():
    """Factory-defaulted fields are optional, so they stay out of `required`."""
    schema = _get_response_component_schema(FactoryDefaults, path="/factory")
    assert "required" not in schema or schema.get("required") in (None, [])


def test_factory_defaults_match_msgspec_schema():
    """Bolt's component schema for factory defaults matches `msgspec.json.schema`."""
    bolt_props = _get_response_component_schema(FactoryDefaults, path="/factory")["properties"]
    msgspec_props = msgspec.json.schema_components((FactoryDefaults,))[1]["FactoryDefaults"]["properties"]
    for field_name in ("name", "tags", "meta"):
        assert bolt_props[field_name]["default"] == msgspec_props[field_name]["default"]


def test_arbitrary_default_factory_emits_no_default():
    """A non-builtin factory (e.g. `datetime.now`) emits NO `default`, matching msgspec.

    msgspec only materializes the four builtin mutable factories
    (`list`/`dict`/`set`/`bytearray`); every other factory gets no `default`.
    Materializing one here would both diverge from msgspec and freeze a value
    (e.g. a generation-time timestamp) into the spec.
    """

    class WithDynamicFactory(msgspec.Struct):
        created: datetime.datetime = msgspec.field(default_factory=datetime.datetime.now)

    props = _get_response_component_schema(WithDynamicFactory, path="/dyn")["properties"]
    assert "default" not in props["created"]
    msgspec_props = msgspec.json.schema_components((WithDynamicFactory,))[1]["WithDynamicFactory"]["properties"]
    assert "default" not in msgspec_props["created"]


def test_nonencodable_default_factory_does_not_break_schema_generation():
    """A factory returning a non-JSON-encodable object must not crash schema generation.

    Materializing such a factory into `default` would embed an unencodable object
    and 500 the whole `/docs/openapi.json` endpoint. The whitelist avoids calling
    it at all, so generation succeeds and the field simply carries no `default`.
    """

    class Opaque:
        pass

    class WithOpaqueFactory(msgspec.Struct):
        thing: Opaque = msgspec.field(default_factory=Opaque)

    props = _get_response_component_schema(WithOpaqueFactory, path="/opaque")["properties"]
    assert "default" not in props["thing"]


def test_every_factory_kind_matches_msgspec_default():
    """Across every factory kind, bolt's `default` presence + value matches msgspec.

    msgspec is the correctness oracle here. It materializes the four builtin
    mutable factories (`list`/`dict`/`set`/`bytearray`); stores immutable
    factories (`tuple`/`frozenset`) as a plain `default`; and emits NO default
    for arbitrary factories. This pins all three behaviors at once — and would
    catch the whitelist being narrowed (e.g. dropping `set`/`bytearray`) or
    widened (materializing `datetime.now`). Both sides are normalized through
    the same JSON encode so set→[] / bytearray→"" compare like-for-like.
    """

    class EveryFactory(msgspec.Struct):
        as_list: list = msgspec.field(default_factory=list)
        as_dict: dict = msgspec.field(default_factory=dict)
        as_set: set = msgspec.field(default_factory=set)
        as_bytearray: bytearray = msgspec.field(default_factory=bytearray)
        as_tuple: tuple = msgspec.field(default_factory=tuple)
        as_frozenset: frozenset = msgspec.field(default_factory=frozenset)
        as_dynamic: datetime.datetime = msgspec.field(default_factory=datetime.datetime.now)

    def as_json(value):
        return msgspec.json.decode(msgspec.json.encode(value))

    bolt_props = _get_response_component_schema(EveryFactory, path="/every")["properties"]
    msgspec_props = msgspec.json.schema_components((EveryFactory,))[1]["EveryFactory"]["properties"]

    sentinel = object()
    for field_name in ("as_list", "as_dict", "as_set", "as_bytearray", "as_tuple", "as_frozenset", "as_dynamic"):
        bolt_default = bolt_props[field_name].get("default", sentinel)
        ms_field = msgspec_props[field_name]
        if "default" in ms_field:
            assert bolt_default == as_json(ms_field["default"]), field_name
        else:
            assert bolt_default is sentinel, f"{field_name} should carry no default (matching msgspec)"

    # Concrete spot-checks so the parity assertion can't pass vacuously.
    assert bolt_props["as_set"]["default"] == []
    assert bolt_props["as_bytearray"]["default"] == ""
    assert bolt_props["as_tuple"]["default"] == []
    assert "default" not in bolt_props["as_dynamic"]


# ---------- named enums promoted to components -----------------------
#
# Regression tests for #246. Named enum classes (`enum.Enum` / msgspec
# `EnumType` / Django `TextChoices`) used in body/response schemas are now
# promoted to named `#/components/schemas/<Name>` components + `$ref`, parallel
# to how Structs are handled — so consumers get a reusable type and the enum's
# docstring survives as `description`. Anonymous `Literal[...]` unions stay
# inline; query-param enums (covered above) also stay inline.


class GateReason(enum.StrEnum):
    """Why a feature is off."""

    entitlement = "entitlement"
    capability = "capability"


class UndocumentedColor(enum.StrEnum):
    red = "red"
    green = "green"


def test_named_enum_field_emits_ref_in_component_schema():
    """An enum field inside a response struct becomes a `$ref`, not inline."""

    class FeatureState(msgspec.Struct):
        reason: GateReason

    props = _get_response_component_schema(FeatureState)["properties"]
    assert props["reason"] == {"$ref": "#/components/schemas/GateReason"}


def test_named_enum_registers_component_with_description():
    """The promoted enum component carries its values, title, and docstring."""

    class FeatureState(msgspec.Struct):
        reason: GateReason

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/feature")
    async def get_feature() -> FeatureState:
        pass

    components = _get_schema(api)["components"]["schemas"]
    assert "GateReason" in components
    enum_component = components["GateReason"]
    assert enum_component["title"] == "GateReason"
    assert enum_component["description"] == "Why a feature is off."
    assert enum_component["type"] == "string"
    assert set(enum_component["enum"]) == {"entitlement", "capability"}


def test_named_enum_without_docstring_omits_description():
    """An undocumented enum component carries no `description` field."""

    class Palette(msgspec.Struct):
        color: UndocumentedColor

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/palette")
    async def get_palette() -> Palette:
        pass

    component = _get_schema(api)["components"]["schemas"]["UndocumentedColor"]
    assert "description" not in component
    assert component["title"] == "UndocumentedColor"


def test_bare_enum_response_model_emits_ref_and_component():
    """`-> GateReason` (a bare enum, not a struct field) becomes a `$ref` + component.

    Struct *fields* reach the msgspec.inspect enum branch; a bare/nested enum
    response annotation arrives via the typing path and used to fall through to
    `{"type": "object"}`. It must promote like any other named enum.
    """
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/reason")
    async def get_reason() -> GateReason:
        pass

    schema = _get_schema(api)
    resp = schema["paths"]["/reason"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert resp == {"$ref": "#/components/schemas/GateReason"}
    component = schema["components"]["schemas"]["GateReason"]
    assert component["description"] == "Why a feature is off."
    assert component["type"] == "string"
    assert set(component["enum"]) == {"entitlement", "capability"}


def test_bare_enum_inside_list_response_model_emits_ref():
    """`-> list[GateReason]` recurses to the same `$ref` for its item type."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/reasons")
    async def get_reasons() -> list[GateReason]:
        pass

    schema = _get_schema(api)
    resp = schema["paths"]["/reasons"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert resp["type"] == "array"
    assert resp["items"] == {"$ref": "#/components/schemas/GateReason"}
    assert "GateReason" in schema["components"]["schemas"]


def test_nullable_named_enum_field_refs_inside_anyof():
    """`GateReason | None = None` → `anyOf: [$ref, null]` with `default: null`.

    Matches the shape `msgspec.json.schema_components` produces.
    """

    class FeatureState(msgspec.Struct):
        reason: GateReason | None = None

    schema = _get_response_component_schema(FeatureState)
    reason = schema["properties"]["reason"]
    assert reason["anyOf"] == [
        {"$ref": "#/components/schemas/GateReason"},
        {"type": "null"},
    ]
    assert reason["default"] is None


def test_django_text_choices_promoted_to_component():
    """A Django `TextChoices` field (msgspec `CustomType`) is promoted too."""

    class Task(msgspec.Struct):
        status: DjangoStatus

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/task")
    async def get_task() -> Task:
        pass

    components = _get_schema(api)["components"]["schemas"]
    assert components["Task"]["properties"]["status"] == {"$ref": "#/components/schemas/DjangoStatus"}
    assert components["DjangoStatus"]["type"] == "string"
    assert set(components["DjangoStatus"]["enum"]) == {"planned", "active", "completed"}


def test_anonymous_literal_field_stays_inline():
    """An anonymous `Literal[...]` field is NOT promoted — it stays inline.

    Guards the boundary the design hinges on: only named enum *classes* become
    components; anonymous literal unions have no name to register under.
    """

    class SortSpec(msgspec.Struct):
        order: Literal["asc", "desc"] = "asc"

    schema = _get_response_component_schema(SortSpec)
    order = schema["properties"]["order"]
    assert "$ref" not in order
    assert order["type"] == "string"
    assert set(order["enum"]) == {"asc", "desc"}
    # No component should have been registered for the anonymous literal.
    assert set(schema["properties"]) == {"order"}


def test_query_param_enum_stays_inline():
    """Query-param enums are NOT promoted (inline context); existing behavior holds."""

    class FilterQuery(msgspec.Struct):
        status: RegularEnum | None = None

    status_schema = _get_query_param_schema(FilterQuery, "status")
    inner = status_schema
    assert "$ref" not in inner
    assert inner["type"] == "string"
    assert set(inner["enum"]) == {"active", "inactive"}


def test_same_named_types_from_different_modules_coexist():
    """Two distinct same-named types from different modules both get components.

    Component names are a flat namespace, but a clash on the short `__name__`
    expands *both* colliding types to their `module.qualname` (matching
    `msgspec.json.schema_components`) so neither silently steals the other's
    `$ref`. The bare name is not used; each `$ref` resolves to the right shape;
    `title` stays the short class name.
    """

    class Status(enum.StrEnum):  # noqa: F811 - intentional short-name clash with the struct below
        ok = "ok"

    Status.__module__ = "app_a.api"
    StatusEnum = Status

    class Status(msgspec.Struct):  # noqa: F811 - intentional short-name clash with the enum above
        code: int

    Status.__module__ = "app_b.api"
    StatusStruct = Status

    class Wrapper(msgspec.Struct):
        state: StatusEnum
        detail: StatusStruct

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/wrapper")
    async def get_wrapper() -> Wrapper:
        pass

    components = _get_schema(api)["components"]["schemas"]
    state_key = components["Wrapper"]["properties"]["state"]["$ref"].split("/")[-1]
    detail_key = components["Wrapper"]["properties"]["detail"]["$ref"].split("/")[-1]

    assert "Status" not in components  # bare name claimed by neither
    assert state_key != detail_key  # both coexist under distinct (qualified) names
    assert components[state_key]["type"] == "string" and set(components[state_key]["enum"]) == {"ok"}
    assert components[detail_key]["properties"]["code"]["type"] == "integer"
    # Qualified component *key*, but the short class name still on `title`.
    assert components[state_key]["title"] == "Status"
    assert components[detail_key]["title"] == "Status"


def test_genuinely_indistinguishable_types_raise():
    """Two types with the *same* module + qualname can't be told apart — raise.

    Distinct types that share both `__name__` and `module.qualname` (e.g. two
    classes defined under the same name in the same scope) have no name that
    separates them. Qualifying both still collides, so generation raises a
    `ComponentNameCollisionError` (a `ValueError`) rather than emit a `$ref`
    that resolves to the wrong shape. `msgspec.json.schema_components` itself
    fails (KeyError) on this case; we fail with a clear, typed error.
    """

    class Status(enum.StrEnum):  # noqa: F811 - intentional clash with the struct below
        ok = "ok"

    StatusEnum = Status

    class Status(msgspec.Struct):  # noqa: F811 - intentional clash with the enum above
        code: int

    StatusStruct = Status
    # Same scope ⇒ identical __qualname__ and __module__ ⇒ indistinguishable.
    assert StatusEnum.__qualname__ == StatusStruct.__qualname__
    assert StatusEnum.__module__ == StatusStruct.__module__

    class Wrapper(msgspec.Struct):
        state: StatusEnum
        detail: StatusStruct

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/wrapper")
    async def get_wrapper() -> Wrapper:
        pass

    generator = SchemaGenerator(api, api._openapi_config)
    with pytest.raises(ComponentNameCollisionError) as exc_info:
        generator.generate()
    assert {exc_info.value.new_type, exc_info.value.existing_type} == {StatusEnum, StatusStruct}
    assert "OpenAPI component name collision" in str(exc_info.value)


def test_same_named_enum_reused_across_fields_does_not_falsely_collide():
    """The same enum on multiple fields is reuse, not a collision — no raise."""

    class TwoStages(msgspec.Struct):
        primary: GateReason
        secondary: GateReason

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/twostages")
    async def get_two() -> TwoStages:
        pass

    schema = _get_schema(api)
    assert "GateReason" in schema["components"]["schemas"]


def test_shared_enum_reuses_single_component():
    """The same enum on two fields registers one component and two `$ref`s."""

    class TwoGates(msgspec.Struct):
        primary: GateReason
        secondary: GateReason

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/gates")
    async def get_gates() -> TwoGates:
        pass

    schema = _get_schema(api)
    components = schema["components"]["schemas"]
    ref = {"$ref": "#/components/schemas/GateReason"}
    assert components["TwoGates"]["properties"]["primary"] == ref
    assert components["TwoGates"]["properties"]["secondary"] == ref
    # Exactly one shared enum component, not one per use site.
    assert sum(1 for name in components if name == "GateReason") == 1
