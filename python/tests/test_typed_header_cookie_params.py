"""Typed Header() and Cookie() parameters are converted and validated in Rust.

Path and query values arrive in the handler as their declared types, and a bad
value gives a 422. Header and cookie values must behave the same way.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.params import Cookie, Header
from django_bolt.testing import TestClient


class TypedHeaders(msgspec.Struct):
    x_count: int
    x_ratio: float = 1.0


class TypedCookies(msgspec.Struct):
    count: int
    flag: bool = False


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.get("/header/int")
    async def header_int(x_count: Annotated[int, Header()]):
        return {"value": x_count, "type": type(x_count).__name__}

    @api.get("/header/optional")
    async def header_optional(x_limit: Annotated[int, Header()] = 10):
        return {"value": x_limit, "type": type(x_limit).__name__}

    @api.get("/header/none")
    async def header_none(x_limit: Annotated[int | None, Header()] = None):
        return {"value": x_limit, "type": type(x_limit).__name__}

    @api.get("/header/bool")
    async def header_bool(x_debug: Annotated[bool, Header()]):
        return {"value": x_debug, "type": type(x_debug).__name__}

    @api.get("/header/alias")
    async def header_alias(count: Annotated[int, Header(alias="X-Item-Count")]):
        return {"value": count, "type": type(count).__name__}

    @api.get("/header/uuid")
    async def header_uuid(x_request_id: Annotated[uuid.UUID, Header()]):
        return {"value": str(x_request_id), "type": type(x_request_id).__name__}

    @api.get("/header/datetime")
    async def header_datetime(x_since: Annotated[datetime, Header()]):
        return {"value": x_since.isoformat(), "type": type(x_since).__name__}

    @api.get("/header/str")
    async def header_str(x_name: Annotated[str, Header()]):
        return {"value": x_name, "type": type(x_name).__name__}

    @api.get("/header/sync")
    def header_sync(x_count: Annotated[int, Header()]):
        return {"value": x_count, "type": type(x_count).__name__}

    @api.get("/header/request")
    async def header_request(request, x_count: Annotated[int, Header()]):
        return {"value": x_count, "meta": request.META.get("HTTP_X_COUNT")}

    @api.get("/header/struct")
    async def header_struct(headers: Annotated[TypedHeaders, Header()]):
        return {"count": headers.x_count, "ratio": headers.x_ratio}

    # An untyped header whose wire name equals a typed query name must stay a string.
    @api.get("/query-named-like-header")
    async def query_named_like_header(request, accept: int = 0):
        return {"accept": accept, "header": request.headers.get("accept")}

    @api.get("/cookie/int")
    async def cookie_int(count: Annotated[int, Cookie()]):
        return {"value": count, "type": type(count).__name__}

    @api.get("/cookie/optional")
    async def cookie_optional(page: Annotated[int, Cookie()] = 1):
        return {"value": page, "type": type(page).__name__}

    @api.get("/cookie/bool")
    async def cookie_bool(dark_mode: Annotated[bool, Cookie()]):
        return {"value": dark_mode, "type": type(dark_mode).__name__}

    @api.get("/cookie/alias")
    async def cookie_alias(count: Annotated[int, Cookie(alias="item-count")]):
        return {"value": count, "type": type(count).__name__}

    @api.get("/cookie/str")
    async def cookie_str(session: Annotated[str, Cookie()]):
        return {"value": session, "type": type(session).__name__}

    @api.get("/cookie/struct")
    async def cookie_struct(cookies: Annotated[TypedCookies, Cookie()]):
        return {"count": cookies.count, "flag": cookies.flag}

    with TestClient(api) as test_client:
        yield test_client


def _detail(response) -> str:
    return str(response.json()["detail"])


class TestTypedHeaders:
    def test_int_header_is_converted(self, client):
        response = client.get("/header/int", headers={"X-Count": "5"})
        assert response.status_code == 200, response.text
        assert response.json() == {"value": 5, "type": "int"}

    def test_invalid_int_header_gives_422_naming_the_header(self, client):
        response = client.get("/header/int", headers={"X-Count": "abc"})
        assert response.status_code == 422, response.text
        detail = _detail(response)
        assert "x-count" in detail
        assert "abc" in detail

    def test_missing_required_header_gives_422(self, client):
        response = client.get("/header/int")
        assert response.status_code == 422, response.text
        assert "x-count" in _detail(response)

    def test_optional_header_with_default(self, client):
        assert client.get("/header/optional").json() == {"value": 10, "type": "int"}
        response = client.get("/header/optional", headers={"X-Limit": "25"})
        assert response.json() == {"value": 25, "type": "int"}

    def test_optional_header_rejects_bad_value(self, client):
        response = client.get("/header/optional", headers={"X-Limit": "many"})
        assert response.status_code == 422, response.text
        assert "x-limit" in _detail(response)

    def test_optional_none_header(self, client):
        assert client.get("/header/none").json() == {"value": None, "type": "NoneType"}
        response = client.get("/header/none", headers={"X-Limit": "7"})
        assert response.json() == {"value": 7, "type": "int"}

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False), ("off", False)],
    )
    def test_bool_header(self, client, raw, expected):
        response = client.get("/header/bool", headers={"X-Debug": raw})
        assert response.status_code == 200, response.text
        assert response.json() == {"value": expected, "type": "bool"}

    def test_invalid_bool_header(self, client):
        response = client.get("/header/bool", headers={"X-Debug": "maybe"})
        assert response.status_code == 422, response.text
        assert "x-debug" in _detail(response)

    def test_aliased_header(self, client):
        response = client.get("/header/alias", headers={"X-Item-Count": "3"})
        assert response.json() == {"value": 3, "type": "int"}
        response = client.get("/header/alias", headers={"X-Item-Count": "three"})
        assert response.status_code == 422, response.text
        assert "x-item-count" in _detail(response)

    def test_uuid_header(self, client):
        value = uuid.uuid4()
        response = client.get("/header/uuid", headers={"X-Request-Id": str(value)})
        assert response.json() == {"value": str(value), "type": "UUID"}
        assert client.get("/header/uuid", headers={"X-Request-Id": "nope"}).status_code == 422

    def test_datetime_header(self, client):
        response = client.get("/header/datetime", headers={"X-Since": "2024-01-02T03:04:05"})
        assert response.json() == {"value": "2024-01-02T03:04:05", "type": "datetime"}
        assert client.get("/header/datetime", headers={"X-Since": "yesterday"}).status_code == 422

    def test_str_header_is_unchanged(self, client):
        response = client.get("/header/str", headers={"X-Name": "123"})
        assert response.json() == {"value": "123", "type": "str"}

    def test_sync_handler(self, client):
        assert client.get("/header/sync", headers={"X-Count": "9"}).json() == {"value": 9, "type": "int"}
        assert client.get("/header/sync", headers={"X-Count": "x"}).status_code == 422

    def test_request_meta_keeps_typed_header(self, client):
        response = client.get("/header/request", headers={"X-Count": "5"})
        assert response.json() == {"value": 5, "meta": "5"}

    def test_struct_header(self, client):
        response = client.get("/header/struct", headers={"X-Count": "4", "X-Ratio": "0.5"})
        assert response.status_code == 200, response.text
        assert response.json() == {"count": 4, "ratio": 0.5}
        response = client.get("/header/struct", headers={"X-Count": "four"})
        assert response.status_code == 422, response.text
        assert "x-count" in _detail(response)

    def test_typed_query_does_not_convert_header_with_same_wire_name(self, client):
        response = client.get("/query-named-like-header?accept=3", headers={"Accept": "text/html"})
        assert response.status_code == 200, response.text
        assert response.json() == {"accept": 3, "header": "text/html"}


class TestTypedCookies:
    def test_int_cookie_is_converted(self, client):
        response = client.get("/cookie/int", cookies={"count": "5"})
        assert response.status_code == 200, response.text
        assert response.json() == {"value": 5, "type": "int"}

    def test_invalid_int_cookie_gives_422_naming_the_cookie(self, client):
        response = client.get("/cookie/int", cookies={"count": "abc"})
        assert response.status_code == 422, response.text
        detail = _detail(response)
        assert "count" in detail
        assert "abc" in detail

    def test_optional_cookie_with_default(self, client):
        assert client.get("/cookie/optional").json() == {"value": 1, "type": "int"}
        assert client.get("/cookie/optional", cookies={"page": "3"}).json() == {"value": 3, "type": "int"}
        assert client.get("/cookie/optional", cookies={"page": "last"}).status_code == 422

    def test_bool_cookie(self, client):
        assert client.get("/cookie/bool", cookies={"dark_mode": "true"}).json() == {"value": True, "type": "bool"}
        assert client.get("/cookie/bool", cookies={"dark_mode": "0"}).json() == {"value": False, "type": "bool"}
        assert client.get("/cookie/bool", cookies={"dark_mode": "maybe"}).status_code == 422

    def test_aliased_cookie(self, client):
        response = client.get("/cookie/alias", cookies={"item-count": "8"})
        assert response.json() == {"value": 8, "type": "int"}
        response = client.get("/cookie/alias", cookies={"item-count": "eight"})
        assert response.status_code == 422, response.text
        assert "item-count" in _detail(response)

    def test_str_cookie_is_unchanged(self, client):
        response = client.get("/cookie/str", cookies={"session": "42"})
        assert response.json() == {"value": "42", "type": "str"}

    def test_struct_cookie(self, client):
        response = client.get("/cookie/struct", cookies={"count": "2", "flag": "yes"})
        assert response.status_code == 200, response.text
        assert response.json() == {"count": 2, "flag": True}
