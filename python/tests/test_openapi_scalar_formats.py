"""The OpenAPI schema gives each typed scalar its string format in every place it occurs.

A form body, a form struct and the parameters take the raw Python type. A
JSON body struct takes the ``msgspec.inspect`` node. Both paths must give
the same schema, or code generators see ``object`` (``dynamic`` in Dart). (#337)
"""

# No ``from __future__ import annotations``: the handlers take the parametrized
# ``scalar`` from their closure, so their annotations must be evaluated.
import datetime
import decimal
import enum
import uuid
from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.param_functions import Cookie, Form, Header, Query

SCALARS = [
    (datetime.datetime, {"type": "string", "format": "date-time"}),
    (datetime.date, {"type": "string", "format": "date"}),
    (datetime.time, {"type": "string", "format": "time"}),
    (datetime.timedelta, {"type": "string", "format": "duration"}),
    (uuid.UUID, {"type": "string", "format": "uuid"}),
    (decimal.Decimal, {"type": "string", "format": "decimal"}),
]
SCALAR_IDS = [scalar.__name__ for scalar, _ in SCALARS]


def _spec(api: BoltAPI) -> dict:
    return SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate().to_schema()


def _parameter_schemas(spec: dict, path: str) -> dict[str, dict]:
    return {parameter["name"]: parameter["schema"] for parameter in spec["paths"][path]["get"]["parameters"]}


def _form_schema(spec: dict, path: str) -> dict:
    return spec["paths"][path]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]


@pytest.mark.parametrize(("scalar", "expected"), SCALARS, ids=SCALAR_IDS)
def test_a_path_query_header_and_cookie_parameter_has_the_format_of_its_type(scalar, expected):
    api = BoltAPI()

    @api.get("/items/{item}")
    async def get_item(
        item: scalar,
        since: Annotated[scalar, Query()],
        x_since: Annotated[scalar, Header(alias="x-since")],
        since_cookie: Annotated[scalar, Cookie()],
    ):
        return {}

    schemas = _parameter_schemas(_spec(api), "/items/{item}")

    assert schemas == {"item": expected, "since": expected, "x-since": expected, "since_cookie": expected}


@pytest.mark.parametrize(("scalar", "expected"), SCALARS, ids=SCALAR_IDS)
def test_an_optional_query_parameter_has_the_format_of_its_type(scalar, expected):
    api = BoltAPI()

    @api.get("/items")
    async def list_items(since: scalar | None = None):
        return {}

    assert _parameter_schemas(_spec(api), "/items")["since"] == expected


@pytest.mark.parametrize(("scalar", "expected"), SCALARS, ids=SCALAR_IDS)
def test_a_form_field_has_the_format_of_its_type(scalar, expected):
    api = BoltAPI()

    @api.post("/items")
    async def create_item(value: Annotated[scalar, Form()]):
        return {}

    assert _form_schema(_spec(api), "/items")["properties"]["value"] == expected


@pytest.mark.parametrize(("scalar", "expected"), SCALARS, ids=SCALAR_IDS)
def test_a_json_body_struct_field_has_the_format_of_its_type(scalar, expected):
    api = BoltAPI()

    Item = msgspec.defstruct("Item", [("value", scalar)])

    @api.post("/items")
    async def create_item(item: Item):
        return {}

    assert _spec(api)["components"]["schemas"]["Item"]["properties"]["value"] == expected


class UserGender(enum.StrEnum):
    MALE = "male"
    FEMALE = "female"


class CompleteUserData(msgspec.Struct, gc=False):
    birth_day: datetime.datetime
    height: Annotated[float, msgspec.Meta(ge=130)]
    gender: UserGender = UserGender.MALE


def test_a_form_struct_documents_each_field_as_the_json_body_does():
    """The struct of #337: its form fields match its JSON body fields, apart from the enum reference."""
    api = BoltAPI()

    @api.post("/json")
    async def complete_json(data: CompleteUserData):
        return {}

    @api.post("/form")
    async def complete_form(data: Annotated[CompleteUserData, Form()]):
        return {}

    spec = _spec(api)
    form = _form_schema(spec, "/form")

    assert form["properties"]["birth_day"] == {"type": "string", "format": "date-time"}
    assert form["properties"]["height"] == {"type": "number", "minimum": 130}
    assert form["required"] == ["birth_day", "height"]

    json_properties = spec["components"]["schemas"]["CompleteUserData"]["properties"]
    assert json_properties["birth_day"] == form["properties"]["birth_day"]
    assert json_properties["height"] == form["properties"]["height"]
