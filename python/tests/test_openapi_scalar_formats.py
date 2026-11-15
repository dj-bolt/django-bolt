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
import json
import uuid
from typing import Annotated

import msgspec
import pytest
from openapi_spec_validator import validate

from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.param_functions import Cookie, File, Form, Header, Query
from django_bolt.testing import TestClient

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


class Attachment(msgspec.Struct):
    data: bytes
    preview: bytearray
    view: memoryview


def test_bytes_are_base64_text_in_json_and_binary_in_a_file_part():
    """msgspec writes bytes in JSON as base64 text. A file part of a multipart body holds the raw bytes."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))
    base64_text = {"type": "string", "contentEncoding": "base64"}
    binary = {"type": "string", "format": "binary"}

    @api.get("/attachment")
    async def get_attachment() -> Attachment:
        return Attachment(data=b"\x00\x01hi", preview=bytearray(b"hi"), view=memoryview(b"hi"))

    @api.get("/chunks")
    async def get_chunks() -> list[bytes]:
        return [b"\x00\x01hi"]

    @api.post("/upload")
    async def upload(raw: Annotated[bytes, File()], parts: Annotated[list[bytes], File()]):
        return {}

    api._register_openapi_routes()
    with TestClient(api) as client:
        spec = client.get("/docs/openapi.json").json()
        assert client.get("/attachment").json() == {"data": "AAFoaQ==", "preview": "aGk=", "view": "aGk="}
        assert client.get("/chunks").json() == ["AAFoaQ=="]

    validate(spec)
    assert spec["openapi"] == "3.1.0"
    assert spec["components"]["schemas"]["Attachment"]["properties"] == {
        "data": base64_text,
        "preview": base64_text,
        "view": base64_text,
    }
    chunks = spec["paths"]["/chunks"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert chunks == {"type": "array", "items": base64_text}
    assert _form_schema(spec, "/upload")["properties"] == {"raw": binary, "parts": {"type": "array", "items": binary}}


class TokenParams(msgspec.Struct):
    token: bytes
    tags: list[bytes] = []
    hint: bytes | None = None


def test_bytes_in_a_parameter_are_plain_text():
    """A bytes parameter gets the UTF-8 bytes of the text value. Only a JSON body holds base64 text."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))
    text = {"type": "string"}
    seen = {}

    @api.get("/scalars/{part}")
    async def scalars(
        part: bytes,
        key: bytes,
        keys: list[bytes],
        hint: bytes | None = None,
        x_key: Annotated[bytes, Header()] = b"",
        session: Annotated[bytes, Cookie()] = b"",
    ):
        seen.update(part=part, key=key, keys=keys, x_key=x_key, session=session)
        return {}

    @api.get("/query-model")
    async def query_model(params: Annotated[TokenParams, Query()]):
        seen.update(token=params.token, tags=params.tags)
        return {}

    @api.get("/header-model")
    async def header_model(params: Annotated[TokenParams, Header()]):
        return {}

    @api.get("/cookie-model")
    async def cookie_model(params: Annotated[TokenParams, Cookie()]):
        return {}

    @api.post("/form")
    async def form(name: Annotated[bytes, Form()], raw: Annotated[bytes, File()]):
        return {}

    @api.post("/form-model")
    async def form_model(params: Annotated[TokenParams, Form()]):
        return {}

    @api.post("/json")
    async def json_body(body: Attachment):
        return {}

    api._register_openapi_routes()
    with TestClient(api) as client:
        spec = client.get("/docs/openapi.json").json()
        # The wire form that the schema describes.
        response = client.get(
            "/scalars/p%C3%A9?key=abc&keys=a&keys=b", headers={"x-key": "hdr", "cookie": "session=sid"}
        )
        assert response.status_code == 200
        assert seen == {"part": "pé".encode(), "key": b"abc", "keys": [b"a", b"b"], "x_key": b"hdr", "session": b"sid"}
        assert client.get("/query-model?token=abc&tags=a&tags=b").status_code == 200
        assert (seen["token"], seen["tags"]) == (b"abc", [b"a", b"b"])

    validate(spec)
    model_fields = {
        "token": text,
        "tags": {"type": "array", "items": text, "default": []},
        "hint": {"anyOf": [text, {"type": "null"}], "default": None},
    }

    def parameters(path):
        return {(p["in"], p["name"]): p["schema"] for p in spec["paths"][path]["get"]["parameters"]}

    assert parameters("/scalars/{part}") == {
        ("path", "part"): text,
        ("query", "key"): text,
        ("query", "keys"): {"type": "array", "items": text},
        ("query", "hint"): text,
        ("header", "x-key"): {**text, "default": ""},
        ("cookie", "session"): {**text, "default": ""},
    }
    assert parameters("/query-model") == {("query", name): schema for name, schema in model_fields.items()}
    assert parameters("/header-model")[("header", "params")]["properties"] == model_fields
    assert parameters("/cookie-model")[("cookie", "params")]["properties"] == model_fields
    # A form field is text. A file part stays binary.
    assert _form_schema(spec, "/form")["properties"] == {"name": text, "raw": {"type": "string", "format": "binary"}}
    form_model_fields = _form_schema(spec, "/form-model")["properties"]
    assert form_model_fields["token"] == text
    assert form_model_fields["tags"] == {"type": "array", "items": text, "default": []}
    assert "contentEncoding" not in json.dumps(form_model_fields)
    # A bytes field of a JSON body is base64 text.
    assert spec["components"]["schemas"]["Attachment"]["properties"]["data"] == {
        "type": "string",
        "contentEncoding": "base64",
    }


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
