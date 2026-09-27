"""The OpenAPI schema of parameters with rich types, and of the remaining msgspec types.

- A sequence parameter or form field is an array, as Bolt now takes each value.
- A ``NewType`` or a ``type`` alias documents the type it names.
- ``msgspec.Meta`` constraints of a parameter show in its schema.
- A dataclass or a ``TypedDict`` body field is an object component, and a
  ``NamedTuple`` is an array component, as msgspec encodes them.
- ``Any`` and ``msgspec.Raw`` allow any value (``{}``), and ``None`` is ``null``.
"""

# No ``from __future__ import annotations``: the handlers use local types.
import dataclasses
from typing import Annotated, Any, NamedTuple, NewType, TypedDict

import msgspec
from openapi_spec_validator import validate

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.param_functions import Cookie, Form, Header, Query

UserId = NewType("UserId", int)
type Page = int
type MaybePage = Page | None

INTEGER = {"type": "integer"}
STRING = {"type": "string"}


def _spec(api: BoltAPI) -> dict:
    spec = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate().to_schema()
    validate(spec)
    return spec


def _parameters(spec: dict, path: str) -> dict[str, dict]:
    return {parameter["name"]: parameter["schema"] for parameter in spec["paths"][path]["get"]["parameters"]}


def test_a_sequence_parameter_is_an_array():
    api = BoltAPI()

    @api.get("/items")
    async def items(
        tags: Annotated[list[int], Query()],
        unique: Annotated[set[int], Query()],
        names: Annotated[frozenset[str], Query()],
        steps: Annotated[tuple[int, ...], Query()],
        point: Annotated[tuple[int, str], Query()],
        x_ids: Annotated[list[UserId], Header(alias="x-ids")],
        flags: Annotated[set[str], Cookie()],
    ):
        return {}

    assert _parameters(_spec(api), "/items") == {
        "tags": {"type": "array", "items": INTEGER},
        "unique": {"type": "array", "items": INTEGER, "uniqueItems": True},
        "names": {"type": "array", "items": STRING, "uniqueItems": True},
        "steps": {"type": "array", "items": INTEGER},
        "point": {"type": "array", "prefixItems": [INTEGER, STRING], "minItems": 2, "maxItems": 2},
        "x-ids": {"type": "array", "items": INTEGER},
        "flags": {"type": "array", "items": STRING, "uniqueItems": True},
    }


def test_a_sequence_form_field_is_an_array():
    api = BoltAPI()

    @api.post("/items")
    async def create(tags: Annotated[set[int], Form()], pair: Annotated[tuple[int, int], Form()]):
        return {}

    schema = _spec(api)["paths"]["/items"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert schema["properties"] == {
        "tags": {"type": "array", "items": INTEGER, "uniqueItems": True},
        "pair": {"type": "array", "prefixItems": [INTEGER, INTEGER], "minItems": 2, "maxItems": 2},
    }


def test_a_newtype_or_type_alias_parameter_documents_its_base_type():
    api = BoltAPI()

    @api.get("/users/{user_id}")
    async def user(user_id: UserId, page: Page = 1, next_page: MaybePage = None):
        return {}

    assert _parameters(_spec(api), "/users/{user_id}") == {
        "user_id": INTEGER,
        "page": {"type": "integer", "default": 1},
        "next_page": INTEGER,
    }


def test_the_constraints_of_a_parameter_show_in_its_schema():
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def item(
        item_id: Annotated[int, msgspec.Meta(ge=1)],
        code: Annotated[str, msgspec.Meta(pattern="^[A-Z]{3}$", max_length=3), Query()],
        x_ratio: Annotated[float, msgspec.Meta(gt=0, lt=1), Header(alias="x-ratio")],
        tags: Annotated[list[str], msgspec.Meta(max_length=2), Query()],
    ):
        return {}

    assert _parameters(_spec(api), "/items/{item_id}") == {
        "item_id": {"type": "integer", "minimum": 1},
        "code": {"type": "string", "maxLength": 3, "pattern": "^[A-Z]{3}$"},
        "x-ratio": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
        "tags": {"type": "array", "items": STRING, "maxItems": 2},
    }


@dataclasses.dataclass
class Address:
    """A postal address."""

    city: str
    zip_code: str = "00000"


class Dimensions(TypedDict):
    width: int
    height: int


class Point(NamedTuple):
    """A point on a grid."""

    x: int
    y: int = 0


class Shipment(msgspec.Struct):
    address: Address
    size: Dimensions
    origin: Point
    ids: list[UserId]
    note: Any
    raw: msgspec.Raw
    nothing: None
    blob: bytearray


def test_the_remaining_msgspec_types_of_a_json_body():
    api = BoltAPI()

    @api.post("/shipments")
    async def ship(shipment: Shipment):
        return {}

    components = _spec(api)["components"]["schemas"]

    assert components["Shipment"]["properties"] == {
        "address": {"$ref": "#/components/schemas/Address"},
        "size": {"$ref": "#/components/schemas/Dimensions"},
        "origin": {"$ref": "#/components/schemas/Point"},
        "ids": {"type": "array", "items": INTEGER},
        "note": {},
        "raw": {},
        "nothing": {"type": "null"},
        "blob": {"type": "string", "format": "binary"},
    }
    assert components["Address"] == {
        "title": "Address",
        "description": "A postal address.",
        "type": "object",
        "properties": {"city": STRING, "zip_code": {"type": "string", "default": "00000"}},
        "required": ["city"],
    }
    dimensions = components["Dimensions"]
    # msgspec gives the keys of a TypedDict in its own order.
    assert sorted(dimensions.pop("required")) == ["height", "width"]
    assert dimensions == {"title": "Dimensions", "type": "object", "properties": {"width": INTEGER, "height": INTEGER}}
    assert components["Point"] == {
        "title": "Point",
        "description": "A point on a grid.",
        "type": "array",
        "prefixItems": [INTEGER, {"type": "integer", "default": 0}],
        "minItems": 1,
        "maxItems": 2,
    }


def test_a_dataclass_with_no_docstring_has_no_generated_description():
    """``dataclasses`` writes a signature as ``__doc__``. That is not a description."""

    @dataclasses.dataclass
    class Plain:
        value: int

    class Holder(msgspec.Struct):
        plain: Plain

    api = BoltAPI()

    @api.post("/plain")
    async def plain(holder: Holder):
        return {}

    assert "description" not in _spec(api)["components"]["schemas"]["Plain"]
