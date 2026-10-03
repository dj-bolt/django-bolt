"""Rust type conversion of path, query and form parameters by wire name.

Rust converts a value by the key that arrives on the wire. These tests make
sure that a parameter with an alias still gets its declared type, and that
two parameters that read one wire key cannot declare two types.
Typed headers and cookies have their own tests in test_typed_header_cookie_params.py.
"""

from __future__ import annotations

from typing import Annotated

import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.param_functions import Cookie, Form, Header, Path, Query
from django_bolt.testing import TestClient, WebSocketTestClient


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.get("/query")
    async def aliased_query(page: Annotated[int, Query(alias="p")]):
        return {"page": page, "type": type(page).__name__}

    @api.get("/query-sync")
    def aliased_query_sync(page: Annotated[int, Query(alias="p")]):
        return {"page": page, "type": type(page).__name__}

    @api.get("/items/{id}")
    async def aliased_path(item_id: Annotated[int, Path(alias="id")]):
        return {"item_id": item_id, "type": type(item_id).__name__}

    @api.post("/form")
    async def aliased_form(count: Annotated[int, Form(alias="n")]):
        return {"count": count, "type": type(count).__name__}

    @api.get("/swapped")
    async def swapped(
        p: Annotated[str, Query(alias="page")],
        page: Annotated[int, Query(alias="p")],
    ):
        return {"p": p, "page": page, "page_type": type(page).__name__}

    with TestClient(api) as test_client:
        yield test_client


@pytest.mark.parametrize("url", ["/query", "/query-sync"])
def test_aliased_query_param_is_converted(client, url):
    response = client.get(f"{url}?p=3")
    assert response.status_code == 200, response.text
    assert response.json() == {"page": 3, "type": "int"}


@pytest.mark.parametrize("url", ["/query", "/query-sync"])
def test_aliased_query_param_rejects_bad_value(client, url):
    response = client.get(f"{url}?p=abc")
    assert response.status_code == 422, response.text
    assert "'p'" in response.text


def test_aliased_path_param_is_converted(client):
    response = client.get("/items/5")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": 5, "type": "int"}


def test_aliased_path_param_rejects_bad_value(client):
    response = client.get("/items/abc")
    assert response.status_code == 422, response.text


def test_aliased_form_field_is_converted(client):
    response = client.post("/form", data={"n": "7"})
    assert response.status_code == 200, response.text
    assert response.json() == {"count": 7, "type": "int"}


def test_aliased_form_field_rejects_bad_value(client):
    response = client.post("/form", data={"n": "abc"})
    assert response.status_code == 422, response.text


def test_field_name_equal_to_other_alias_keeps_each_type(client):
    """Each field gets the type of the field that reads that wire key."""
    response = client.get("/swapped?p=3&page=abc")
    assert response.status_code == 200, response.text
    assert response.json() == {"p": "abc", "page": 3, "page_type": "int"}


def _register_clash(first, second):
    """Register a handler with parameters ``a`` and ``b`` of the given annotations."""

    async def clash(a, b):
        return {}

    clash.__annotations__ = {"a": first, "b": second}
    BoltAPI().get("/clash")(clash)


@pytest.mark.parametrize(
    ("first", "second", "key"),
    [
        (Annotated[str, Query()], Annotated[int, Query(alias="a")], "'a'"),
        (Annotated[int, Path(alias="id")], Annotated[str, Query(alias="id")], "'id'"),
        (Annotated[int, Header(alias="X-N")], Annotated[str, Header(alias="x_n")], "'x-n'"),
        (Annotated[int, Cookie(alias="n")], Annotated[bool, Cookie(alias="n")], "'n'"),
    ],
    ids=["query-name-vs-alias", "path-vs-query", "header-case-and-underscore", "cookie"],
)
def test_two_types_for_one_wire_key_fail_at_registration(first, second, key):
    """Rust converts one wire value once, so two types for one key cannot both apply."""
    with pytest.raises(TypeError) as exc_info:
        _register_clash(first, second)

    message = str(exc_info.value)
    assert key in message
    assert "'a'" in message
    assert "'b'" in message


def test_one_name_in_different_sources_is_not_a_clash():
    """Headers, cookies and path/query have separate maps, so each keeps its own type."""
    _register_clash(Annotated[int, Query(alias="n")], Annotated[str, Cookie(alias="n")])


def test_same_type_for_one_wire_key_is_allowed():
    api = BoltAPI()

    @api.get("/shared")
    async def shared(a: Annotated[int, Query(alias="n")], b: Annotated[int, Query(alias="n")]):
        return {"a": a, "b": b}

    with TestClient(api) as test_client:
        response = test_client.get("/shared?n=2")
    assert response.status_code == 200, response.text
    assert response.json() == {"a": 2, "b": 2}


@pytest.fixture(scope="module")
def ws_api():
    api = BoltAPI()

    @api.websocket("/ws/rooms/{id}")
    async def room(
        websocket: WebSocket,
        room_id: Annotated[int, Path(alias="id")],
        page: Annotated[int, Query(alias="p")],
    ):
        await websocket.accept()
        await websocket.send_json(
            {
                "room_id": room_id,
                "room_id_type": type(room_id).__name__,
                "page": page,
                "page_type": type(page).__name__,
            }
        )

    return api


@pytest.mark.asyncio
async def test_websocket_aliased_params_are_converted(ws_api):
    async with WebSocketTestClient(ws_api, "/ws/rooms/9", query_string="p=3") as ws:
        response = await ws.receive_json()
    assert response == {"room_id": 9, "room_id_type": "int", "page": 3, "page_type": "int"}
