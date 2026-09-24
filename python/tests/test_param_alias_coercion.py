"""Rust type coercion of parameters whose wire name is not the Python name.

Rust converts path, query, header, cookie and form values by the key that
arrives on the wire. These tests make sure that an alias, or the header name
form (``x_count`` -> ``x-count``), still gets the declared type.
"""

from __future__ import annotations

from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.param_functions import Cookie, Form, Header, Path, Query
from django_bolt.testing import TestClient, WebSocketTestClient


class CountHeaders(msgspec.Struct):
    x_count: int


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

    @api.get("/cookie")
    async def aliased_cookie(count: Annotated[int, Cookie(alias="n")]):
        return {"count": count, "type": type(count).__name__}

    @api.get("/header")
    async def plain_header(x_count: Annotated[int, Header()]):
        return {"x_count": x_count, "type": type(x_count).__name__}

    @api.get("/header-alias")
    async def aliased_header(count: Annotated[int, Header(alias="X-Num")]):
        return {"count": count, "type": type(count).__name__}

    @api.get("/header-struct")
    async def header_struct(headers: Annotated[CountHeaders, Header()]):
        return {"x_count": headers.x_count, "type": type(headers.x_count).__name__}

    @api.post("/form")
    async def aliased_form(count: Annotated[int, Form(alias="n")]):
        return {"count": count, "type": type(count).__name__}

    @api.get("/cookie-plain")
    async def plain_cookie(n: Annotated[int, Cookie()]):
        return {"n": n}

    @api.get("/shared-name")
    async def shared_name(request, id: int, theme: Annotated[str, Cookie()], x_token: Annotated[str, Header()]):
        return {"id": id, "cookie_id": request.cookies["id"], "header_id": request.headers["id"]}

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


def test_aliased_cookie_is_converted(client):
    client.cookies.set("n", "7")
    try:
        response = client.get("/cookie")
    finally:
        client.cookies.clear()
    assert response.status_code == 200, response.text
    assert response.json() == {"count": 7, "type": "int"}


def test_header_is_converted_by_its_header_name(client):
    response = client.get("/header", headers={"x-count": "4"})
    assert response.status_code == 200, response.text
    assert response.json() == {"x_count": 4, "type": "int"}


def test_header_rejects_bad_value(client):
    response = client.get("/header", headers={"x-count": "abc"})
    assert response.status_code == 422, response.text


def test_aliased_header_is_converted(client):
    response = client.get("/header-alias", headers={"X-Num": "4"})
    assert response.status_code == 200, response.text
    assert response.json() == {"count": 4, "type": "int"}


def test_header_struct_field_is_converted(client):
    response = client.get("/header-struct", headers={"x-count": "4"})
    assert response.status_code == 200, response.text
    assert response.json() == {"x_count": 4, "type": "int"}


def test_cookie_rejects_bad_value(client):
    client.cookies.set("n", "abc")
    try:
        response = client.get("/cookie-plain")
    finally:
        client.cookies.clear()
    assert response.status_code == 422, response.text
    assert "'n'" in response.text


def test_query_type_does_not_apply_to_header_or_cookie_of_same_name(client):
    """Each source has its own type map, so an unrelated header or cookie stays a string."""
    client.cookies.set("id", "tracking")
    client.cookies.set("theme", "dark")
    try:
        response = client.get("/shared-name?id=5", headers={"id": "abc", "x-token": "t"})
    finally:
        client.cookies.clear()
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 5, "cookie_id": "tracking", "header_id": "abc"}


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


def test_two_types_for_one_wire_key_fail_at_registration():
    """Rust converts one wire value once, so two types for one key cannot both apply."""
    api = BoltAPI()

    with pytest.raises(TypeError) as exc_info:

        @api.get("/clash")
        async def clash(p: Annotated[str, Query()], page: Annotated[int, Query(alias="p")]):
            return {}

    message = str(exc_info.value)
    assert "'p'" in message
    assert "'page'" in message


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
