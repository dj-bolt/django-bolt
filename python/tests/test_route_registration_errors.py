"""Startup refuses a route that can never get a request, and names the routes in the error."""

from __future__ import annotations

import re

import pytest

from django_bolt import BoltAPI, _core
from django_bolt.testing import TestClient

CONFLICT_FIX = "Keep one of the two routes, or add a fixed segment before the parameter in one path."
PARAM_FIX = "has a parameter with no name, or a brace with no pair. Write each parameter as {name} or {name:type}."


def add(api: BoltAPI, spec: str, label: str) -> None:
    """Add the route of ``spec`` ("GET /path", "WS /path" or "NATIVE GET /path") to ``api``.

    An HTTP handler returns ``label``, so a test sees which route served a request.
    """
    kind, _, rest = spec.partition(" ")
    if kind == "WS":

        async def ws_handler(websocket) -> None:
            await websocket.accept()

        ws_handler.__name__ = f"ws_{label}"
        api.websocket(rest)(ws_handler)
    elif kind == "NATIVE":
        method, path = rest.split(" ")
        api.native_route(method, path, _core.native_test_capsule())
    else:

        async def handler(request) -> dict:
            return {"route": label}

        handler.__name__ = f"handler_{label}"
        getattr(api, kind.lower())(rest)(handler)


def api_with(first: str, second: str, trailing_slash: str = "strip") -> BoltAPI:
    api = BoltAPI(trailing_slash=trailing_slash)
    add(api, first, "first")
    add(api, second, "second")
    return api


@pytest.mark.parametrize(
    ("trailing_slash", "first", "second", "requests"),
    [
        ("strip", "GET /files/{file_id}", "POST /files/{file_path:path}", [("GET /files/a", "first"), ("POST /files/a/b", "second")]),
        ("strip", "GET /articles/{slug}", "PUT /articles/{article_id}", [("GET /articles/a", "first"), ("PUT /articles/a", "second")]),
        ("strip", "GET /authors/{username}", "GET /authors/me", [("GET /authors/ana", "first"), ("GET /authors/me", "second")]),
        ("strip", "GET /files/{p:path}", "GET /files/readme", [("GET /files/a/b", "first"), ("GET /files/readme", "second")]),
        ("strip", "GET /files/{p:path}", "GET /files/x/{id}", [("GET /files/y/1", "first"), ("GET /files/x/1", "second")]),
        ("strip", "GET /a/{x}/edit", "GET /a/{y}/delete", [("GET /a/1/edit", "first"), ("GET /a/1/delete", "second")]),
        ("strip", "GET /a/{x}", "GET /a/{y}/comments", [("GET /a/1", "first"), ("GET /a/1/comments", "second")]),
        ("strip", "GET /{a}", "GET /{b}/{p:path}", [("GET /q", "first"), ("GET /q/r/s", "second")]),
        ("strip", "GET /{a:path}", "GET /files/{b:path}", [("GET /x/y", "first"), ("GET /files/y", "second")]),
        ("strip", "GET /{p:path}", "GET /", [("GET /x", "first"), ("GET /", "second")]),
        ("strip", "GET /files/{id}", "GET /files/v{id}", [("GET /files/1", "first"), ("GET /files/v1", "second")]),
        ("strip", "GET /o", "OPTIONS /o", [("GET /o", "first"), ("OPTIONS /o", "second")]),
        ("strip", "WS /live", "GET /live", [("GET /live", "second")]),
        ("strip", "WS /live/{room}", "GET /live/{p:path}", [("GET /live/a/b", "second")]),
        ("keep", "GET /tags", "GET /tags/", [("GET /tags", "first"), ("GET /tags/", "second")]),
        ("keep", "GET /tags/{id}", "GET /tags/{slug}/", [("GET /tags/1", "first"), ("GET /tags/1/", "second")]),
    ],
)  # fmt: skip
def test_routes_that_do_not_conflict_each_serve_their_requests(trailing_slash, first, second, requests):
    with TestClient(api_with(first, second, trailing_slash)) as client:
        for request, label in requests:
            method, path = request.split(" ")
            response = client.request(method, path)
            assert response.status_code == 200, (request, response.text)
            assert response.json() == {"route": label}, request


def conflict(route: str, earlier: str) -> str:
    return f"Failed to register route: {route} conflicts with {earlier}. {CONFLICT_FIX}"


def already_registered(route: str) -> str:
    return f"Failed to register route: {route} is already registered"


@pytest.mark.parametrize(
    ("trailing_slash", "first", "second", "message"),
    [
        # A parameter and a catch-all at one position, in each order.
        ("strip", "GET /files/{file_id}", "GET /files/{file_path:path}",
         conflict("GET /files/{file_path:path}", "GET /files/{file_id}")),
        ("strip", "GET /files/{file_path:path}", "GET /files/{file_id}",
         conflict("GET /files/{file_id}", "GET /files/{file_path:path}")),
        ("strip", "GET /{p:path}", "GET /{a}/{b}", conflict("GET /{a}/{b}", "GET /{p:path}")),
        ("keep", "GET /tags/{id}/", "GET /tags/{p:path}", conflict("GET /tags/{p:path}", "GET /tags/{id}/")),
        # Two parameters or two catch-alls at one position.
        ("strip", "GET /articles/{slug}", "GET /articles/{article_id}",
         conflict("GET /articles/{article_id}", "GET /articles/{slug}")),
        ("strip", "GET /items/{id:int}", "GET /items/{id:str}", conflict("GET /items/{id:str}", "GET /items/{id:int}")),
        ("strip", "GET /files/{a:path}", "GET /files/{b:path}", conflict("GET /files/{b:path}", "GET /files/{a:path}")),
        # The same method and path.
        ("strip", "GET /items", "GET /items", already_registered("GET /items")),
        ("strip", "GET /items/{id}", "GET /items/{id}", already_registered("GET /items/{id}")),
        ("strip", "GET /tags", "GET /tags/", already_registered("GET /tags")),
        ("strip", "GET /tags/{id}", "GET /tags/{id}/", already_registered("GET /tags/{id}")),
        ("append", "GET /tags", "GET /tags/", already_registered("GET /tags/")),
        # A malformed path.
        ("strip", "GET /x", "GET /files/{p:path}/meta",
         "Failed to register route: GET /files/{p:path}/meta has a {name:path} parameter before the end of the "
         "path. Move it to the end."),
        ("strip", "GET /x", "GET /files/{a}-{b}",
         "Failed to register route: GET /files/{a}-{b} has two parameters in one segment. "
         "Use one parameter for each segment."),
        ("strip", "GET /x", "GET /e/{}", f"Failed to register route: GET /e/{{}} {PARAM_FIX}"),
        ("strip", "GET /x", "GET /e/{id", f"Failed to register route: GET /e/{{id {PARAM_FIX}"),
        # WebSocket routes. The server removes the trailing slash of each WebSocket path.
        ("strip", "WS /live", "WS /live", already_registered("WEBSOCKET /live")),
        ("strip", "WS /live", "WS /live/", already_registered("WEBSOCKET /live")),
        ("keep", "WS /live", "WS /live/", already_registered("WEBSOCKET /live")),
        ("strip", "WS /live/{room}", "WS /live/{room}", already_registered("WEBSOCKET /live/{room}")),
        ("strip", "WS /live/{room}", "WS /live/{channel}",
         conflict("WEBSOCKET /live/{channel}", "WEBSOCKET /live/{room}")),
        ("strip", "WS /live/{room}", "WS /live/{p:path}", conflict("WEBSOCKET /live/{p:path}", "WEBSOCKET /live/{room}")),
        ("strip", "WS /x", "WS /w/{room", f"Failed to register route: WEBSOCKET /w/{{room {PARAM_FIX}"),
    ],
)  # fmt: skip
def test_routes_that_can_never_get_a_request_stop_startup(trailing_slash, first, second, message):
    api = api_with(first, second, trailing_slash)
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        TestClient(api)


def mounted(path: str) -> BoltAPI:
    child = BoltAPI()
    add(child, f"GET {path}", path)
    return child


def parent_route_and_mount() -> BoltAPI:
    api = BoltAPI()
    add(api, "GET /m/{id}", "parent")
    api.mount("/m", mounted("/{p:path}"))
    return api


def two_mounts() -> BoltAPI:
    api = BoltAPI()
    api.mount("/m", mounted("/{id}"))
    api.mount("/m", mounted("/{p:path}"))
    return api


def python_route_and_native_route() -> BoltAPI:
    api = BoltAPI()
    add(api, "GET /m/{id}", "python")
    add(api, "NATIVE GET /m/{p:path}", "native")
    return api


@pytest.mark.parametrize("make_api", [parent_route_and_mount, two_mounts, python_route_and_native_route])
def test_a_mount_and_a_native_route_give_the_message_of_a_route(make_api):
    message = conflict("GET /m/{p:path}", "GET /m/{id}")
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        TestClient(make_api())
