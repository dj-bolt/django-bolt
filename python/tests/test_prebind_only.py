"""Routes whose arguments Rust binds from its own request maps.

A route with only path, query, header and cookie parameters, and no other
reader of the request, gets ``prebind_only``. Rust then binds the handler
arguments from the Rust maps and builds no Python source dicts. The
behaviour must equal the dict path: typed values, defaults, ``None`` for an
optional value with no default, and the 422 of the Python injector for a
missing required value.
"""

from __future__ import annotations

from typing import Annotated

import pytest

from django_bolt import BoltAPI
from django_bolt.param_functions import Cookie, Header, Query
from django_bolt.testing import TestClient


def _route_meta(api: BoltAPI, path: str) -> dict:
    for _method, route_path, handler_id, _fn in api._routes:
        if route_path == path:
            return api._handler_middleware.get(handler_id, {})
    raise AssertionError(f"route {path} not registered")


@pytest.fixture(scope="module")
def api() -> BoltAPI:
    api = BoltAPI()

    @api.get("/typed")
    async def typed(
        x_count: Annotated[int, Header()],
        page: Annotated[int, Query(alias="p")] = 1,
        flag: Annotated[bool, Cookie()] = False,
    ) -> dict:
        return {"x_count": x_count, "page": page, "flag": flag}

    @api.get("/optional")
    async def optional(x_maybe: Annotated[int | None, Header()], limit: int | None = None) -> dict:
        return {"x_maybe": x_maybe, "limit": limit}

    @api.get("/with-request")
    async def with_request(request, x_count: Annotated[int, Header()]) -> dict:
        return {"x_count": x_count, "seen": request.headers.get("x-count"), "n": len(request.headers)}

    @api.get("/sync")
    def sync_route(x_count: Annotated[int, Header()], count: Annotated[int, Cookie()] = 0) -> dict:
        return {"x_count": x_count, "count": count}

    return api


@pytest.fixture(scope="module")
def client(api: BoltAPI):
    with TestClient(api) as client:
        yield client


def test_registration_marks_routes_without_other_readers(api: BoltAPI) -> None:
    assert _route_meta(api, "/typed")["prebind_only"] is True
    assert _route_meta(api, "/optional")["prebind_only"] is True
    assert _route_meta(api, "/sync")["prebind_only"] is True
    assert "prebind_only" not in _route_meta(api, "/with-request")


def test_python_middleware_keeps_the_dicts() -> None:
    class NoOpMiddleware:
        def __init__(self, get_response):
            self.get_response = get_response

        async def __call__(self, request):
            return await self.get_response(request)

    api = BoltAPI(middleware=[NoOpMiddleware])

    @api.get("/behind-middleware")
    async def behind_middleware(x_count: Annotated[int, Header()]) -> dict:
        return {"x_count": x_count}

    assert "prebind_only" not in _route_meta(api, "/behind-middleware")


def test_typed_values_bind_from_rust_maps(client: TestClient) -> None:
    response = client.get("/typed?p=3", headers={"X-Count": "5", "Cookie": "flag=yes"})
    assert response.status_code == 200
    assert response.json() == {"x_count": 5, "page": 3, "flag": True}


def test_defaults_apply_when_values_are_absent(client: TestClient) -> None:
    response = client.get("/typed", headers={"X-Count": "5"})
    assert response.status_code == 200
    assert response.json() == {"x_count": 5, "page": 1, "flag": False}


def test_optional_without_default_is_none(client: TestClient) -> None:
    assert client.get("/optional").json() == {"x_maybe": None, "limit": None}
    response = client.get("/optional?limit=2", headers={"X-Maybe": "7"})
    assert response.json() == {"x_maybe": 7, "limit": 2}


def test_missing_required_value_is_422(client: TestClient) -> None:
    response = client.get("/typed?p=3")
    assert response.status_code == 422
    assert response.json()["detail"] == "Missing required header: x-count"


def test_bad_typed_value_is_422(client: TestClient) -> None:
    response = client.get("/typed", headers={"X-Count": "abc"})
    assert response.status_code == 422
    assert response.json()["detail"] == "Header 'x-count': Invalid integer 'abc': invalid digit found in string"


def test_sync_route_binds_from_rust_maps(client: TestClient) -> None:
    response = client.get("/sync", headers={"X-Count": "3", "Cookie": "count=9"})
    assert response.json() == {"x_count": 3, "count": 9}


def test_request_parameter_still_sees_the_headers(client: TestClient) -> None:
    response = client.get("/with-request", headers={"X-Count": "3", "X-Other": "z"})
    body = response.json()
    assert body["x_count"] == 3
    assert body["seen"] == 3
    assert body["n"] >= 2
