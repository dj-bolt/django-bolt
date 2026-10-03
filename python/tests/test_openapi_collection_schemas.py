"""The OpenAPI schema of a set, frozenset or tuple field in a JSON body is an array.

msgspec decodes each of these from a JSON array. Before, the schema said
``object``, and code generators made an untyped value. The expected schemas
are the ones ``msgspec.json.schema`` gives, apart from ``items: false``: the
``maxItems`` of a fixed tuple already rejects more items.
"""

# No ``from __future__ import annotations``: ``msgspec.defstruct`` takes the
# parametrized ``field_type`` as a value.
from typing import Annotated

import msgspec
import pytest
from openapi_spec_validator import validate

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator

INTEGER = {"type": "integer"}
STRING = {"type": "string"}

COLLECTIONS = [
    pytest.param(set[int], {"type": "array", "items": INTEGER, "uniqueItems": True}, id="set"),
    pytest.param(frozenset[str], {"type": "array", "items": STRING, "uniqueItems": True}, id="frozenset"),
    pytest.param(tuple[int, ...], {"type": "array", "items": INTEGER}, id="variable_tuple"),
    pytest.param(
        tuple[int, str],
        {"type": "array", "prefixItems": [INTEGER, STRING], "minItems": 2, "maxItems": 2},
        id="fixed_tuple",
    ),
    pytest.param(tuple[()], {"type": "array", "minItems": 0, "maxItems": 0}, id="empty_tuple"),
    pytest.param(
        Annotated[set[int], msgspec.Meta(min_length=1, max_length=3)],
        {"type": "array", "items": INTEGER, "uniqueItems": True, "minItems": 1, "maxItems": 3},
        id="set_with_lengths",
    ),
    pytest.param(
        Annotated[tuple[int, ...], msgspec.Meta(max_length=2)],
        {"type": "array", "items": INTEGER, "maxItems": 2},
        id="variable_tuple_with_lengths",
    ),
    pytest.param(
        Annotated[list[int], msgspec.Meta(min_length=1)],
        {"type": "array", "items": INTEGER, "minItems": 1},
        id="list_with_lengths",
    ),
]


def _field_schema(field_type) -> dict:
    api = BoltAPI()
    Item = msgspec.defstruct("Item", [("value", field_type)])

    @api.post("/items")
    async def create_item(item: Item):
        return {}

    spec = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate().to_schema()
    return spec["components"]["schemas"]["Item"]["properties"]["value"]


@pytest.mark.parametrize(("field_type", "expected"), COLLECTIONS)
def test_a_collection_field_of_a_json_body_is_an_array(field_type, expected):
    assert _field_schema(field_type) == expected


class Point(msgspec.Struct):
    x: int
    y: int


def test_the_struct_items_of_a_tuple_are_a_component():
    assert _field_schema(tuple[Point, ...]) == {"type": "array", "items": {"$ref": "#/components/schemas/Point"}}


def test_a_spec_with_each_collection_is_valid_openapi():
    api = BoltAPI()
    fields = [(f"value_{index}", param.values[0]) for index, param in enumerate(COLLECTIONS)]
    Everything = msgspec.defstruct("Everything", [*fields, ("points", tuple[Point, ...])])

    @api.post("/everything")
    async def create_everything(item: Everything):
        return {}

    validate(SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate().to_schema())
