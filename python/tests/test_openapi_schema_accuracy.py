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


class RegularEnum(str, enum.Enum):
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


def test_int_ge_constraint():
    """Test that an annotated int with a ge constraint produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def get_items(query: Annotated[ConstrainedFilters, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    page = _get_param(params, "page")
    assert page["schema"]["type"] == "integer"
    assert page["schema"]["minimum"] == 1
    assert "exclusiveMinimum" not in page["schema"]


def test_int_ge_le_constraints():
    """Test that an annotated int with a ge and le constraint produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def get_items(query: Annotated[ConstrainedFilters, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    size = _get_param(params, "size")
    assert size["schema"]["minimum"] == 1
    assert size["schema"]["maximum"] == 100


def test_float_gt_lt_constraints():
    """Test that an annotated float with a gt and lt constraint produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def get_items(query: Annotated[ConstrainedFilters, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    ratio = _get_param(params, "ratio")
    assert ratio["schema"]["exclusiveMinimum"] == 0.0
    assert ratio["schema"]["exclusiveMaximum"] == 1.0
    assert "minimum" not in ratio["schema"]
    assert "maximum" not in ratio["schema"]


def test_int_multiple_of_constraint():
    """Test that an annotated int with a multiple_of constraint produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def get_items(query: Annotated[ConstrainedFilters, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    step = _get_param(params, "step")
    assert step["schema"]["multipleOf"] == 5


def test_unconstrained_int_has_no_constraint_fields():
    """Test that an unconstrained int produces no constraint fields."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    class SimpleQuery(msgspec.Struct):
        page: int = 1

    @api.get("/items")
    async def get_items(query: Annotated[SimpleQuery, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    page = _get_param(params, "page")
    assert page["schema"]["type"] == "integer"
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        assert key not in page["schema"], f"Unexpected constraint '{key}' on unconstrained int"


def str_min_max_length_constraints():
    """Test that an annotated str with a min_length and max_length constraint produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def get_items(query: Annotated[StringConstrainedQuery, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    name = _get_param(params, "name")
    assert name["schema"]["type"] == "string"
    assert name["schema"]["minLength"] == 1
    assert name["schema"]["maxLength"] == 50


def test_str_pattern_constraint():
    """Test that an annotated str with a pattern constraint produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def get_items(query: Annotated[StringConstrainedQuery, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    code = _get_param(params, "code")
    assert code["schema"]["type"] == "string"
    assert code["schema"]["pattern"] == r"^[A-Z]{3}$"


def test_str_enum_produces_string_enum_schema():
    """Test that an annotated str with an enum constraint produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    class FilterQuery(msgspec.Struct):
        status: RegularEnum | None = None

    @api.get("/items")
    async def get_items(query: Annotated[FilterQuery, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    status = _get_param(params, "status")
    assert status["schema"]["type"] == "string"
    assert set(status["schema"]["enum"]) == {"active", "inactive"}


def test_int_enum_produces_integer_enum_schema():
    """Test that an annotated int with an enum constraint produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    class FilterQuery(msgspec.Struct):
        priority: IntEnum | None = None

    @api.get("/items")
    async def get_items(query: Annotated[FilterQuery, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    priority = _get_param(params, "priority")
    assert priority["schema"]["type"] == "integer"
    assert set(priority["schema"]["enum"]) == {1, 2, 3}


def test_django_text_choices_produces_string_enum():
    """Test that a Django TextChoices enum produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    class FilterQuery(msgspec.Struct):
        status: DjangoStatus | None = None

    @api.get("/items")
    async def get_items(query: Annotated[FilterQuery, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    status = _get_param(params, "status")
    assert status["schema"]["type"] == "string"
    assert set(status["schema"]["enum"]) == {"planned", "active", "completed"}


def test_django_integer_choices_produces_integer_enum():
    """Test that a Django IntegerChoices enum produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    class FilterQuery(msgspec.Struct):
        priority: DjangoPriority | None = None

    @api.get("/items")
    async def get_items(query: Annotated[FilterQuery, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    priority = _get_param(params, "priority")
    assert priority["schema"]["type"] == "integer"
    assert set(priority["schema"]["enum"]) == {1, 2, 3}


def test_literal_string_query_param():
    """Test that a literal string query param produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    class SortQuery(msgspec.Struct):
        order: Literal["asc", "desc"] = "asc"

    @api.get("/items")
    async def get_items(query: Annotated[SortQuery, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    order = _get_param(params, "order")
    assert order["schema"]["type"] == "string"
    assert set(order["schema"]["enum"]) == {"asc", "desc"}


def test_literal_integers_produces_integer_type():
    """Test that a literal integer query param produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    class PageQuery(msgspec.Struct):
        size: Literal[10, 25, 50, 100] = 10

    @api.get("/items")
    async def get_items(query: Annotated[PageQuery, Query()]) -> dict:
        pass

    schema = _get_schema(api)
    params = schema["paths"]["/items"]["get"]["parameters"]
    size = _get_param(params, "size")
    assert size["schema"]["type"] == "integer"
    assert set(size["schema"]["enum"]) == {10, 25, 50, 100}


def test_response_struct_fields_have_defaults():
    """Test that a struct field with a default produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/status")
    async def get_status() -> ResponseWithDefaults:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]
    props = schemas["ResponseWithDefaults"]["properties"]
    assert props["message"]["default"] == "hello"
    assert props["count"]["default"] == 0
    assert props["active"]["default"] is True


def test_response_struct_required_fields_have_no_default():
    """Test that a required struct field with no default produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/item")
    async def get_item() -> NoDefaultsResponse:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]
    props = schemas["NoDefaultsResponse"]["properties"]

    assert "default" not in props["id"]
    assert "default" not in props["name"]
    assert set(schemas["NoDefaultsResponse"]["required"]) == {"id", "name"}


def test_response_struct_reference_field_with_default_none():
    """Test that a reference field with a default of None produces the correct schema."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    class Inner(msgspec.Struct):
        value: str

    class Outer(msgspec.Struct):
        name: str
        inner: Inner | None = None

    @api.get("/item")
    async def get_item() -> Outer:
        pass

    schema = _get_schema(api)
    schemas = schema["components"]["schemas"]
    assert "Outer" in schemas
    assert "Inner" in schemas
