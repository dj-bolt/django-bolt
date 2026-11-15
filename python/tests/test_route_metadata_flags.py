"""Route metadata flags that Rust reads at registration.

Every route sends its request flags and its default status to Rust.
A flag with the wrong type or a missing flag stops registration with an
error that names the key and the route. Rust must not use a default value.
"""

from __future__ import annotations

from functools import wraps

import pytest

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

BOOL_FLAGS = (
    "needs_body",
    "needs_query",
    "needs_headers",
    "needs_cookies",
    "needs_path_params",
    "is_static_route",
    "needs_form_parsing",
)
ALL_FLAGS = (*BOOL_FLAGS, "default_status_code")


def _api_with_route() -> BoltAPI:
    api = BoltAPI()

    @api.get("/items")
    async def items() -> dict:
        return {}

    return api


def _route_meta(api: BoltAPI, path: str) -> dict:
    for _method, route_path, handler_id, _fn in api._routes:
        if route_path == path:
            return api._handler_middleware[handler_id]
    raise AssertionError(f"route {path} not registered")


@pytest.mark.parametrize("key", BOOL_FLAGS)
def test_non_bool_flag_stops_registration(key: str) -> None:
    api = _api_with_route()
    _route_meta(api, "/items")[key] = None

    with pytest.raises(TypeError, match=rf"'{key}'.*GET /items.*NoneType"):
        TestClient(api)


def test_non_int_default_status_code_stops_registration() -> None:
    api = _api_with_route()
    _route_meta(api, "/items")["default_status_code"] = "201"

    with pytest.raises(TypeError, match=r"'default_status_code'.*GET /items.*str"):
        TestClient(api)


@pytest.mark.parametrize("key", ALL_FLAGS)
def test_missing_flag_stops_registration(key: str) -> None:
    api = _api_with_route()
    del _route_meta(api, "/items")[key]

    with pytest.raises(ValueError, match=rf"GET /items.*'{key}'"):
        TestClient(api)


@pytest.mark.parametrize("path", ["/plain", "/items/{item_id}"])
def test_request_only_route_sends_every_flag(path: str) -> None:
    """A handler with only a request parameter has no typed fields.
    It can read any part of the request, so it must parse all of them."""
    api = BoltAPI()

    @api.post(path)
    async def echo(request) -> dict:
        return {
            "body": request.body.decode(),
            "query": dict(request.query),
            "header": request.headers.get("x-probe"),
            "cookie": request.cookies.get("probe"),
            "params": dict(request.params),
        }

    meta = _route_meta(api, path)
    assert {key: meta[key] for key in ALL_FLAGS} == {
        "needs_body": True,
        "needs_query": True,
        "needs_headers": True,
        "needs_cookies": True,
        "needs_path_params": True,
        "is_static_route": "{" not in path,
        "needs_form_parsing": False,
        "default_status_code": 200,
    }

    with TestClient(api) as client:
        response = client.post(
            path.replace("{item_id}", "7") + "?q=1",
            content=b"payload",
            headers={"x-probe": "h", "cookie": "probe=c"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "body": "payload",
        "query": {"q": "1"},
        "header": "h",
        "cookie": "c",
        "params": {"item_id": "7"} if "{" in path else {},
    }


def _repeat_on_route(api: BoltAPI) -> None:
    @api.get("/d/{x}/{x}")
    async def handler(x: int):
        return {"x": x}


def _repeat_on_typed_route(api: BoltAPI) -> None:
    @api.get("/d/{x:int}/sub/{x:path}")
    async def handler(x: int):
        return {"x": x}


def _repeat_with_prefix(_api: BoltAPI) -> None:
    api = BoltAPI(prefix="/d/{x}")

    @api.get("/{x}")
    async def handler(x: int):
        return {"x": x}


def _repeat_on_websocket(api: BoltAPI) -> None:
    @api.websocket("/d/{x}/{x}")
    async def handler(websocket, x: int):
        await websocket.accept()


def _repeat_with_mount(api: BoltAPI) -> None:
    child = BoltAPI()

    @child.get("/{x}")
    async def handler(request):
        return {"x": request.params["x"]}

    api.mount("/d/{x}", child)


@pytest.mark.parametrize(
    "register",
    [_repeat_on_route, _repeat_on_typed_route, _repeat_with_prefix, _repeat_on_websocket, _repeat_with_mount],
)
def test_a_path_with_one_parameter_name_two_times_stops_registration(register) -> None:
    """The router keeps one value for each name, so the first value would be lost."""
    with pytest.raises(ValueError, match="has the parameter 'x' two times"):
        register(BoltAPI())


def test_a_path_with_two_parameter_names_registers() -> None:
    api = BoltAPI()

    @api.get("/d/{x}/{y}")
    async def handler(x: int, y: int):
        return {"x": x, "y": y}

    with TestClient(api) as client:
        assert client.get("/d/1/2").json() == {"x": 1, "y": 2}


def test_websocket_route_sends_every_flag() -> None:
    api = BoltAPI()

    @api.websocket("/ws")
    async def ws(websocket) -> None:
        await websocket.accept()

    handler_id = api._websocket_routes[0][1]
    meta = api._handler_middleware[handler_id]
    assert {key: meta[key] for key in ALL_FLAGS} == {
        "needs_body": False,
        "needs_query": False,
        "needs_headers": False,
        "needs_cookies": False,
        "needs_path_params": False,
        "is_static_route": True,
        "needs_form_parsing": False,
        "default_status_code": 200,
    }


def test_request_only_reads_only_cookies() -> None:
    api = BoltAPI()

    @api.get("/cookie")
    def cookie(request):
        return {"value": request.cookies.get("probe")}

    meta = _route_meta(api, "/cookie")
    assert meta["needs_cookies"] is True
    assert meta["needs_body"] is False
    assert meta["needs_query"] is False
    assert meta["needs_headers"] is False
    with TestClient(api) as client:
        assert client.get("/cookie", headers={"cookie": "probe=value"}).json() == {"value": "value"}


def test_request_only_escape_keeps_components() -> None:
    api = BoltAPI()

    def helper(request):
        return {"cookie": request.cookies["probe"], "header": request.headers["x-probe"]}

    @api.get("/escape")
    def escape(request):
        return helper(request)

    with TestClient(api) as client:
        assert client.get("/escape", headers={"cookie": "probe=c", "x-probe": "h"}).json() == {
            "cookie": "c",
            "header": "h",
        }


def test_request_decorator_keeps_components_outside_handler_body() -> None:
    api = BoltAPI()

    def add_query(handler):
        @wraps(handler)
        def wrapper(request):
            return {**handler(request), "query": request.query["q"]}

        return wrapper

    @api.get("/decorated")
    @add_query
    def decorated(request):
        return {"path": request.path}

    with TestClient(api) as client:
        assert client.get("/decorated?q=value").json() == {"path": "/decorated", "query": "value"}
