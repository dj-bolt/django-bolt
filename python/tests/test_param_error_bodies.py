"""The 422 body for a parameter that fails validation in Python.

Rust coerces scalar values. Python validates a msgspec struct parameter, a
sequence parameter, and a parameter with ``msgspec.Meta`` constraints. Each
error names the source in ``loc`` as a scalar parameter does: ``query``,
``header``, ``cookie`` or ``body``.
"""

from __future__ import annotations

import logging
from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.params import Cookie, Form, Header, Query
from django_bolt.serializers import Serializer, field_validator
from django_bolt.testing import TestClient


class Filters(msgspec.Struct):
    page: int
    ids: list[int] = []
    size: Annotated[int, msgspec.Meta(ge=1)] = 10
    tag: Annotated[str, msgspec.Meta(min_length=2)] = msgspec.field(name="t", default="ok")


class Headers(msgspec.Struct):
    x_api_key: str
    x_count: Annotated[int, msgspec.Meta(ge=1)] = 1
    label: Annotated[str, msgspec.Meta(min_length=2)] = msgspec.field(name="x_label", default="ok")


class Author(Serializer):
    name: str

    @field_validator("name")
    def name_is_not_blank(cls, value):
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.get("/query-struct")
    async def query_struct(filters: Annotated[Filters, Query()]):
        return {"page": filters.page, "ids": filters.ids}

    @api.get("/cookie-struct")
    async def cookie_struct(filters: Annotated[Filters, Cookie()]):
        return {"page": filters.page}

    @api.post("/form-struct")
    async def form_struct(filters: Annotated[Filters, Form()]):
        return {"page": filters.page}

    @api.get("/header-struct")
    async def header_struct(headers: Annotated[Headers, Header()]):
        return {"key": headers.x_api_key}

    @api.get("/sequence")
    async def sequence(ids: Annotated[list[int], Query()]):
        return {"ids": ids}

    @api.get("/constrained")
    async def constrained(n: Annotated[int, msgspec.Meta(ge=1), Query()]):
        return {"n": n}

    @api.post("/body-struct")
    async def body_struct(filters: Filters):
        return {"page": filters.page}

    @api.get("/serializer-query")
    async def serializer_query(author: Annotated[Author, Query()]):
        return {"name": author.name}

    @api.get("/server-serializer")
    async def server_serializer():
        # The server builds the Serializer from its own data, not from the request.
        return {"name": Author(name="   ").name}

    @api.get("/server-error")
    async def server_error():
        raise RuntimeError("server fault")

    with TestClient(api) as client:
        yield client


def _missing(source: str, noun: str, name: str) -> dict:
    return {"type": "missing_field", "loc": [source, name], "msg": f"Missing required {noun}: {name}", "input": None}


@pytest.mark.parametrize(
    ("path", "kwargs", "source", "noun"),
    [
        ("/query-struct", {}, "query", "query parameter"),
        ("/cookie-struct", {}, "cookie", "cookie"),
        ("/form-struct", {"data": {"size": "5"}}, "body", "form field"),
    ],
)
def test_missing_struct_field_names_the_source(client, path, kwargs, source, noun):
    method = client.post if path == "/form-struct" else client.get
    response = method(path, **kwargs)

    assert response.status_code == 422
    assert response.json()["detail"] == [_missing(source, noun, "page")]


class Renamed(msgspec.Struct):
    page_number: int = msgspec.field(name="pageNumber")


def test_missing_renamed_field_uses_its_wire_name():
    api = BoltAPI()

    @api.get("/renamed")
    async def renamed(filters: Annotated[Renamed, Query()]):
        return {"page": filters.page_number}

    with TestClient(api) as client:
        response = client.get("/renamed")

    assert response.status_code == 422
    assert response.json()["detail"] == [_missing("query", "query parameter", "pageNumber")]


def test_missing_header_struct_field_names_the_header(client):
    response = client.get("/header-struct")

    assert response.status_code == 422
    assert response.json()["detail"] == [_missing("header", "header", "x-api-key")]


def test_invalid_header_struct_fields_name_the_headers(client):
    response = client.get("/header-struct", headers={"X-Api-Key": "k", "X-Count": "0", "X-Label": "x"})

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "validation_error", "loc": ["header", "x-count"], "msg": "Expected `int` >= 1", "input": 0},
        {
            "type": "validation_error",
            "loc": ["header", "x-label"],
            "msg": "Expected `str` of length >= 2",
            "input": "x",
        },
    ]


def test_header_struct_reports_a_missing_and_an_invalid_field_together(client):
    response = client.get("/header-struct", headers={"X-Count": "0"})

    assert response.status_code == 422
    assert response.json()["detail"] == [
        _missing("header", "header", "x-api-key"),
        {"type": "validation_error", "loc": ["header", "x-count"], "msg": "Expected `int` >= 1", "input": 0},
    ]


class AuthHeaders(msgspec.Struct):
    token: str = msgspec.field(name="X-Api-Key")


def test_missing_renamed_header_field_names_the_header():
    api = BoltAPI()

    @api.get("/auth")
    async def auth(headers: Annotated[AuthHeaders, Header()]):
        return {"token": headers.token}

    with TestClient(api) as client:
        assert client.get("/auth", headers={"X-Api-Key": "k"}).json() == {"token": "k"}
        response = client.get("/auth")

    assert response.status_code == 422
    assert response.json()["detail"] == [_missing("header", "header", "x-api-key")]


@pytest.fixture
def server_errors(caplog, monkeypatch):
    """Return a function that gives the ERROR records of ``django.server``."""
    # dictConfig() disables each existing logger that its config does not name. Thus a
    # logging test can leave django.server disabled. The tests that expect a record
    # show that the capture works.
    monkeypatch.setattr(logging.getLogger("django.server"), "disabled", False)
    caplog.set_level(logging.ERROR, logger="django.server")

    def records() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == "django.server" and r.levelno >= logging.ERROR]

    return records


@pytest.mark.parametrize(
    "send",
    [
        lambda c: c.get("/header-struct"),
        lambda c: c.get("/query-struct"),
        lambda c: c.get("/sequence?ids=x"),
        lambda c: c.post("/body-struct", json={"page": "x"}),
        lambda c: c.post("/body-struct", content=b"{", headers={"Content-Type": "application/json"}),
        lambda c: c.get("/serializer-query", params={"name": " "}),
    ],
    ids=["header", "query", "sequence", "body", "malformed-body", "serializer-query"],
)
def test_a_request_validation_error_is_not_logged_as_a_server_error(client, server_errors, send):
    # A client can send a bad request on each call. The access log keeps the 422.
    response = send(client)

    assert response.status_code == 422
    assert server_errors() == []


def test_a_validation_error_of_server_data_is_logged(client, server_errors):
    response = client.get("/server-serializer")

    assert response.status_code == 422
    assert server_errors() == [
        "Exception in GET /server-serializer: RequestValidationError: body.name: name must not be blank"
    ]


def test_an_unhandled_exception_is_logged(client, server_errors):
    response = client.get("/server-error")

    assert response.status_code == 500
    assert server_errors() == ["Exception in GET /server-error: RuntimeError: server fault"]


@pytest.mark.parametrize(
    ("path", "send", "source"),
    [
        ("/query-struct", lambda c, p: c.get(p, params={"page": "1", "size": "0", "t": "x"}), "query"),
        ("/form-struct", lambda c, p: c.post(p, data={"page": "1", "size": "0", "t": "x"}), "body"),
    ],
)
def test_invalid_struct_fields_each_get_one_error(client, path, send, source):
    response = send(client, path)

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "validation_error",
            "loc": [source, "size"],
            "msg": "Expected `int` >= 1",
            "input": 0,
        },
        {
            "type": "validation_error",
            "loc": [source, "t"],
            "msg": "Expected `str` of length >= 2",
            "input": "x",
        },
    ]


def test_valid_sequence_field_gets_no_error_when_another_field_fails(client):
    # The sequence values are strings on the wire. Only `size` is wrong.
    response = client.get("/query-struct?page=1&ids=1&ids=2&size=0")

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [["query", "size"]]


def test_invalid_sequence_item_error_points_at_the_item(client):
    response = client.get("/sequence?ids=1&ids=x")

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "validation_error",
            "loc": ["query", "ids"],
            "msg": "Expected `int`, got `str` - at `$[1]`",
            "input": ["1", "x"],
        }
    ]


def test_constrained_scalar_error_has_no_holder_path(client):
    response = client.get("/constrained?n=0")

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "validation_error", "loc": ["query", "n"], "msg": "Expected `int` >= 1", "input": 0}
    ]
