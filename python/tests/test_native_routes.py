"""Native routes: a Rust handler from another extension module serves the request."""

from __future__ import annotations

import asyncio
import ctypes

import pytest
from bolt_mcp import MCP, mount_mcp
from django.core.management.base import CommandError

from django_bolt import BoltAPI, _core
from django_bolt.management.commands.runbolt import Command
from django_bolt.middleware import CompressionConfig, cors
from django_bolt.responses import JSON
from django_bolt.testing import AsyncTestClient, TestClient


def native_api(**kwargs) -> BoltAPI:
    api = BoltAPI(**kwargs)
    api.native_route("GET", "/items/{id}", _core.native_test_capsule())
    api.native_route("POST", "/items/{id}", _core.native_test_capsule())
    return api


def echo(method="GET", path="/items/a%20b", query="q=1", body=""):
    return {
        "method": method,
        "path": path,
        "query": query,
        "params": {"id": "a b"},
        "x_test": "yes",
        "client": "127.0.0.1",
        "body": body,
    }


@pytest.mark.parametrize(
    ("method", "content", "expected"),
    [("GET", None, echo()), ("POST", b"payload", echo("POST", body="payload"))],
)
def test_native_route_gets_the_request_and_sends_the_response(method, content, expected):
    with TestClient(native_api()) as client:
        response = client.request(method, "/items/a%20b", params={"q": "1"}, headers={"x-test": "yes"}, content=content)

    assert response.status_code == 201
    assert response.headers.get_list("x-multi") == ["a", "b"]
    assert response.json() == expected


def test_path_params_are_decoded_once():
    with TestClient(native_api()) as client:
        response = client.get("/items/a%2520b")

    assert response.json()["params"] == {"id": "a%20b"}


def test_native_route_gets_the_client_address_after_trusted_proxies(settings):
    settings.BOLT_TRUSTED_PROXIES = ["127.0.0.1"]
    with TestClient(native_api()) as client:
        response = client.get("/items/1", headers={"x-forwarded-for": "198.51.100.7"})

    assert response.json()["client"] == "198.51.100.7"


@pytest.mark.parametrize(
    "content",
    [b"too long", iter([b"too", b" long"])],
    ids=["content-length", "chunked"],
)
def test_native_route_body_over_the_limit_is_refused(settings, content):
    settings.BOLT_MAX_UPLOAD_SIZE = 4
    with TestClient(native_api()) as client:
        response = client.post("/items/1", content=content)

    assert response.status_code == 413


@pytest.mark.parametrize(
    "headers",
    [{"x-big": "x" * 200}, {f"x-{i}": "1" for i in range(101)}],
    ids=["too-large", "too-many"],
)
def test_native_route_applies_the_header_limits(settings, headers):
    settings.BOLT_MAX_HEADER_SIZE = 100
    with TestClient(native_api()) as client:
        assert client.get("/items/1", headers=headers).status_code == 400


BOLT_500 = {"detail": "Internal Server Error"}


@pytest.mark.parametrize(
    ("case", "status", "body"),
    [
        ("drop", 500, BOLT_500),  # the responder is dropped without a response
        ("status-103", 500, BOLT_500),  # 1xx would desync a keep-alive client
        ("status-999", 500, BOLT_500),
        ("status-404", 404, None),  # the handler's own error stays as it is
        ("status-204", 204, b""),  # no body, also when the handler sends one
        ("status-304", 304, b""),
    ],
)
def test_native_handler_outcomes(case, status, body):
    with TestClient(native_api()) as client:
        response = client.get(f"/items/{case}")

    assert response.status_code == status
    if body is None:
        assert response.json()["params"] == {"id": case}
    elif isinstance(body, bytes):
        assert response.content == body
    else:
        assert response.json() == body


def test_null_response_is_a_server_error():
    api = BoltAPI()
    api.native_route("GET", "/null", _core.native_test_capsule("null_response"))
    with TestClient(api) as client:
        assert client.get("/null").status_code == 500


def test_head_response_has_no_body():
    api = BoltAPI()
    api.native_route("HEAD", "/items/{id}", _core.native_test_capsule())
    with TestClient(api) as client:
        response = client.head("/items/1")

    assert response.status_code == 201
    assert response.content == b""


def _kind(error, request) -> JSON:
    return JSON({"kind": error.kind}, status_code=error.status)


@pytest.mark.parametrize(
    ("path", "status", "body"),
    [
        ("/items/hang", 504, {"kind": "gateway_timeout"}),  # Bolt's error: formatted
        ("/items/drop", 500, {"kind": "internal_error"}),  # Bolt's error: formatted
        ("/items/status-404", 404, None),  # the handler's response: unchanged
    ],
)
def test_error_formatter_formats_only_the_errors_of_bolt(settings, path, status, body):
    if path == "/items/hang":
        # Short only where the handler never completes; the other rows
        # complete from a thread and must not race the timeout.
        settings.BOLT_NATIVE_ROUTE_TIMEOUT = 0.1
    with TestClient(native_api(error_formatter=_kind)) as client:
        response = client.get(path)

    assert response.status_code == status
    if body is None:
        assert response.json()["params"] == {"id": "status-404"}
    else:
        assert response.json() == body


def test_invalid_response_header_is_dropped():
    with TestClient(native_api()) as client:
        response = client.get("/items/bad-header")

    assert response.status_code == 200
    assert "bad header" not in response.headers
    assert response.json()["params"] == {"id": "bad-header"}


def test_native_handler_that_does_not_complete_in_time_gets_504(settings):
    settings.BOLT_NATIVE_ROUTE_TIMEOUT = 0.1
    with TestClient(native_api()) as client:
        assert client.get("/items/hang").status_code == 504


def test_trailing_slash_redirects_to_a_native_route_before_a_catch_all_mount():
    api = native_api()

    async def fallback_asgi(scope, receive, send):
        await send({"type": "http.response.start", "status": 418, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    api.mount_asgi("/", fallback_asgi)

    with TestClient(api) as client:
        # TestClient follows the 308 from /items/1/ to /items/1.
        response = client.get("/items/1/")

    assert response.status_code == 201
    assert response.json()["path"] == "/items/1"


def test_static_python_route_wins_over_a_dynamic_native_route():
    api = native_api()

    @api.get("/items/special")
    async def special():
        return {"from": "python"}

    with TestClient(api) as client:
        assert client.get("/items/special").json() == {"from": "python"}
        assert client.get("/items/7").json()["params"] == {"id": "7"}


def test_native_route_gets_global_cors_and_compression():
    api = native_api(compression=CompressionConfig(backend="gzip"))
    with TestClient(api, use_http_layer=True, cors_allowed_origins=["https://a.example"]) as client:
        response = client.post(
            "/items/1",
            content=b"x" * 2000,
            headers={"Origin": "https://a.example", "Accept-Encoding": "gzip"},
        )

    assert response.headers["access-control-allow-origin"] == "https://a.example"
    assert response.headers["content-encoding"] == "gzip"
    assert response.json()["body"] == "x" * 2000


def test_native_route_does_not_take_the_cors_config_of_a_python_route():
    api = BoltAPI()

    @api.get("/python")
    @cors(origins=["https://custom.com"])
    async def python_route():
        return {}

    api.native_route("GET", "/native", _core.native_test_capsule())

    with TestClient(api, use_http_layer=True) as client:
        python = client.get("/python", headers={"Origin": "https://custom.com"})
        native = client.get("/native", headers={"Origin": "https://custom.com"})

    assert python.headers.get("access-control-allow-origin") == "https://custom.com"
    assert "access-control-allow-origin" not in native.headers


def test_automatic_options_includes_native_routes():
    with TestClient(native_api()) as client:
        response = client.options("/items/1")

    assert response.status_code == 204
    assert response.headers["allow"] == "GET, POST, OPTIONS"


@pytest.mark.parametrize(
    "python_path",
    ["/items/{id}", "/items/{pk}", "/items/{id:int}"],
)
def test_native_route_conflicting_with_a_python_route_stops_startup(python_path):
    api = native_api()

    @api.get(python_path)
    async def python_item():
        return {}

    with pytest.raises(ValueError, match="Failed to register route"):
        TestClient(api)


@pytest.mark.parametrize("path", ["/items/{id}", "/static"])
def test_duplicate_native_route_stops_startup(path):
    api = native_api()
    api.native_route("GET", path, _core.native_test_capsule())
    api.native_route("GET", path, _core.native_test_capsule())
    with pytest.raises(ValueError, match="Failed to register route"):
        TestClient(api)


async def _asgi_app(scope, receive, send):
    pass


@pytest.mark.parametrize(
    "mount",
    [
        lambda api: api.mount_asgi("/hook", _asgi_app),
        lambda api: mount_mcp(api, MCP("native-test", "1.0"), path="/hook"),
    ],
    ids=["asgi", "mcp"],
)
def test_native_route_at_a_mount_path_stops_startup(mount):
    api = BoltAPI()
    api.native_route("POST", "/hook", _core.native_test_capsule())
    mount(api)
    with pytest.raises(ValueError, match="conflicts with an existing HTTP route"):
        TestClient(api)


def _x_route(api: BoltAPI) -> BoltAPI:
    api.native_route("GET", "/x/{id}", _core.native_test_capsule())
    return api


def _mounted() -> BoltAPI:
    parent = BoltAPI()
    parent.mount("/v2", _x_route(BoltAPI()))
    return parent


@pytest.mark.parametrize(
    ("make_api", "path"),
    [
        (lambda: _x_route(BoltAPI(prefix="/api")), "/api/x/1"),
        (lambda: _x_route(BoltAPI(trailing_slash="append")), "/x/1/"),
        (_mounted, "/v2/x/1"),
    ],
    ids=["prefix", "append-slash", "mount"],
)
def test_native_route_path_follows_the_api(make_api, path):
    with TestClient(make_api()) as client:
        response = client.get(path, follow_redirects=False)

    assert response.status_code == 201
    assert response.json()["params"] == {"id": "1"}


def _capsule(name: bytes):
    new = ctypes.pythonapi.PyCapsule_New
    new.restype = ctypes.py_object
    new.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
    return new(1, name, None)


OTHER_NAME = b"other.capsule"


def test_native_route_needs_a_known_method():
    with pytest.raises(ValueError, match="Unsupported HTTP method"):
        BoltAPI().native_route("GTE", "/x", _core.native_test_capsule())


@pytest.mark.parametrize(
    ("capsule", "error", "message"),
    [
        (object, TypeError, "native_handler.v1 capsule"),
        (lambda: _core.native_test_capsule("no_call"), ValueError, "no call function"),
        (lambda: _capsule(OTHER_NAME), TypeError, "native_handler.v1 capsule"),
    ],
    ids=["not-a-capsule", "no-call", "other-name"],
)
def test_invalid_capsule_is_refused_at_registration(capsule, error, message):
    with pytest.raises(error, match=message):
        BoltAPI().native_route("GET", "/x", capsule())


def test_async_test_client_serves_native_routes():
    async def request():
        async with AsyncTestClient(native_api()) as client:
            return await client.get("/items/1")

    assert asyncio.run(request()).status_code == 201


def test_runbolt_refuses_the_same_route_in_two_apis():
    python_api = BoltAPI()

    @python_api.get("/items/{id}")
    async def item():
        return {}

    with pytest.raises(CommandError, match=r"Route conflict: GET /items/\{id\} defined in both a.api and b.api"):
        Command().merge_apis([("a.api", python_api), ("b.api", native_api())])
