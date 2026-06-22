"""
Tests for OpenAPI schema accuracy improvements:
- Annotated constraints (ge/le/gt/lt/multiple_of, min_length/max_length/pattern)
- EnumType and Django TextChoices/IntegerChoices
- Literal type inference (string vs integer vs mixed)
- Struct field defaults in body schemas
"""

import enum
from typing import Annotated, Any, Literal

import msgspec
from django.db import models

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig
from django_bolt.param_functions import Query
from django_bolt.serializers import Serializer
from django_bolt.serializers.types import Email, HttpsURL, PositiveInt
from django_bolt.testing import TestClient


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

    `ratio` is `Annotated[float, ...] | None = None` — under OpenAPI 3.1
    nullability is expressed via `null` in the type union, so the
    constrained float schema is the first arm of `anyOf`.
    """
    ratio_schema = _get_query_param_schema(ConstrainedFilters, "ratio")
    inner = ratio_schema["anyOf"][0]
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

    `code` is `Annotated[str, ...] | None = None` — OpenAPI 3.1 emits
    null in the type union via `anyOf`, so the constrained string is
    the first arm.
    """
    code_schema = _get_query_param_schema(StringConstrainedQuery, "code")
    inner = code_schema["anyOf"][0]
    assert inner["type"] == "string"
    assert inner["pattern"] == r"^[A-Z]{3}$"


def test_str_enum_produces_string_enum_schema():
    """Test that an annotated str with an enum constraint produces the correct schema.

    All four enum-as-nullable tests below use `Foo | None = None`. Under
    OpenAPI 3.1 the null arm appears in `anyOf` alongside the enum.
    """

    class FilterQuery(msgspec.Struct):
        status: RegularEnum | None = None

    status_schema = _get_query_param_schema(FilterQuery, "status")
    inner = status_schema["anyOf"][0]
    assert inner["type"] == "string"
    assert set(inner["enum"]) == {"active", "inactive"}


def test_int_enum_produces_integer_enum_schema():
    """Test that an annotated int with an enum constraint produces the correct schema."""

    class FilterQuery(msgspec.Struct):
        priority: IntEnum | None = None

    priority_schema = _get_query_param_schema(FilterQuery, "priority")
    inner = priority_schema["anyOf"][0]
    assert inner["type"] == "integer"
    assert set(inner["enum"]) == {1, 2, 3}


def test_django_text_choices_produces_string_enum():
    """Test that a Django TextChoices enum produces the correct schema."""

    class FilterQuery(msgspec.Struct):
        status: DjangoStatus | None = None

    status_schema = _get_query_param_schema(FilterQuery, "status")
    inner = status_schema["anyOf"][0]
    assert inner["type"] == "string"
    assert set(inner["enum"]) == {"planned", "active", "completed"}


def test_django_integer_choices_produces_integer_enum():
    """Test that a Django IntegerChoices enum produces the correct schema."""

    class FilterQuery(msgspec.Struct):
        priority: DjangoPriority | None = None

    priority_schema = _get_query_param_schema(FilterQuery, "priority")
    inner = priority_schema["anyOf"][0]
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
