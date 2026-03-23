"""
Tests for OpenAPI schema accuracy improvements:
- Annotated constraints (ge/le/gt/lt/multiple_of, min_length/max_length/pattern)
- EnumType and Django TextChoices/IntegerChoices
- Literal type inference (string vs integer vs mixed)
- Struct field defaults in body schemas
"""

import enum
from typing import Annotated, Literal

import msgspec
from django.db import models

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig
from django_bolt.param_functions import Query
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
    """Test that an annotated float with a gt and lt constraint produces the correct schema."""
    ratio_schema = _get_query_param_schema(ConstrainedFilters, "ratio")
    assert ratio_schema["exclusiveMinimum"] == 0.0
    assert ratio_schema["exclusiveMaximum"] == 1.0
    assert "minimum" not in ratio_schema
    assert "maximum" not in ratio_schema


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
    """Test that an annotated str with a pattern constraint produces the correct schema."""
    code_schema = _get_query_param_schema(StringConstrainedQuery, "code")
    assert code_schema["type"] == "string"
    assert code_schema["pattern"] == r"^[A-Z]{3}$"


def test_str_enum_produces_string_enum_schema():
    """Test that an annotated str with an enum constraint produces the correct schema."""
    class FilterQuery(msgspec.Struct):
        status: RegularEnum | None = None

    status_schema = _get_query_param_schema(FilterQuery, "status")
    assert status_schema["type"] == "string"
    assert set(status_schema["enum"]) == {"active", "inactive"}


def test_int_enum_produces_integer_enum_schema():
    """Test that an annotated int with an enum constraint produces the correct schema."""
    class FilterQuery(msgspec.Struct):
        priority: IntEnum | None = None

    priority_schema = _get_query_param_schema(FilterQuery, "priority")
    assert priority_schema["type"] == "integer"
    assert set(priority_schema["enum"]) == {1, 2, 3}


def test_django_text_choices_produces_string_enum():
    """Test that a Django TextChoices enum produces the correct schema."""
    class FilterQuery(msgspec.Struct):
        status: DjangoStatus | None = None

    status_schema = _get_query_param_schema(FilterQuery, "status")
    assert status_schema["type"] == "string"
    assert set(status_schema["enum"]) == {"planned", "active", "completed"}


def test_django_integer_choices_produces_integer_enum():
    """Test that a Django IntegerChoices enum produces the correct schema."""
    class FilterQuery(msgspec.Struct):
        priority: DjangoPriority | None = None

    priority_schema = _get_query_param_schema(FilterQuery, "priority")
    assert priority_schema["type"] == "integer"
    assert set(priority_schema["enum"]) == {1, 2, 3}


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
    """Test that a reference field with a default of None produces the correct schema."""
    class Inner(msgspec.Struct):
        value: str

    class Outer(msgspec.Struct):
        name: str
        inner: Inner | None = None

    outer = _get_response_component_schema(Outer)
    assert outer["required"] == ["name"]
    inner = outer["properties"]["inner"]
    assert inner["default"] is None
    assert inner["allOf"] == [{"$ref": "#/components/schemas/Inner"}]
