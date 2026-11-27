"""`Vary: Origin` on each response whose CORS answer depends on the `Origin` of the request.

The Fetch standard ("CORS protocol and HTTP caches") asks for `Vary: Origin` when
`Access-Control-Allow-Origin` is not `*` or one static origin. Else a cache can give the
response of one origin, or of a request with no `Origin`, to a CORS request of another origin.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from django.test import override_settings

from django_bolt import BoltAPI, IsAuthenticated, JWTAuthentication, cors, rate_limit, skip_middleware
from django_bolt.responses import FileResponse, Redirect, Response, StreamingResponse
from django_bolt.testing import TestClient

ALLOWED = "https://app.example"
REFUSED = "https://evil.example"
REGEX = r"^https://app\.example$"

# Each config: the CORS settings, the decorator of each route, and how the
# `access-control-allow-origin` value depends on the request.
# "list": the allowed origin only, "echo": each origin, "wildcard": `*`, "off": none.
CONFIGS = {
    "global list": ({"CORS_ALLOWED_ORIGINS": [ALLOWED]}, None, "list"),
    "global regex": ({"CORS_ALLOWED_ORIGIN_REGEXES": [REGEX]}, None, "list"),
    "global wildcard": ({"CORS_ALLOW_ALL_ORIGINS": True}, None, "wildcard"),
    "global wildcard credentials": (
        {"CORS_ALLOW_ALL_ORIGINS": True, "CORS_ALLOW_CREDENTIALS": True},
        None,
        "echo",
    ),
    "route list": ({}, lambda: cors(origins=[ALLOWED]), "list"),
    "route regex": ({}, lambda: cors(origin_regexes=[REGEX]), "list"),
    "route wildcard": ({}, lambda: cors(origins=["*"]), "wildcard"),
    "route wildcard credentials": ({}, lambda: cors(origins=["*"], credentials=True), "echo"),
    "route skips global list": ({"CORS_ALLOWED_ORIGINS": [ALLOWED]}, lambda: skip_middleware("cors"), "off"),
}

PREFLIGHT = {"Access-Control-Request-Method": "GET"}

# Each response: method, path, extra request headers, status, and whether a route answers it.
# A response that no route answers uses the global config only.
RESPONSES = {
    "ok": ("GET", "/ok", {}, 200, True),
    "preflight": ("OPTIONS", "/ok", PREFLIGHT, 204, True),
    "head": ("HEAD", "/ok", {}, 200, True),
    "401 of rust": ("GET", "/auth", {}, 401, True),
    "429 of rust": ("GET", "/limited", {}, 429, True),
    "422": ("GET", "/items/x", {}, 422, True),
    "500": ("GET", "/boom", {}, 500, True),
    "redirect": ("GET", "/redirect", {}, 307, True),
    "stream": ("GET", "/stream", {}, 200, True),
    "file": ("GET", "/file", {}, 200, True),
    "handler vary cookie": ("GET", "/vary-cookie", {}, 200, True),
    "handler vary origin": ("GET", "/vary-origin", {}, 200, True),
    "handler vary star": ("GET", "/vary-star", {}, 200, True),
    "compressed": ("GET", "/big", {"Accept-Encoding": "gzip"}, 200, True),
    "404": ("GET", "/missing", {}, 404, False),
    "preflight of 404 path": ("OPTIONS", "/missing", PREFLIGHT, None, False),
    "mount_asgi": ("GET", "/asgi/x", {}, 200, False),
    "mount_django": ("GET", "/django/x", {}, 200, False),
    "static": ("GET", "/static/a.css", {}, 200, False),
}

ORIGINS = {"no origin": None, "allowed origin": ALLOWED, "refused origin": REFUSED}


async def _asgi_app(scope, receive, send):
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"mounted"})


def _api(route_decorator, file_path: Path) -> BoltAPI:
    api = BoltAPI()

    def route(method, path, **options):
        def register(handler):
            if route_decorator is not None:
                # A wildcard with credentials gives a RuntimeWarning. That config is under test.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    handler = route_decorator()(handler)
            return getattr(api, method)(path, **options)(handler)

        return register

    @route("get", "/ok")
    async def ok() -> dict:
        return {"ok": True}

    @route("head", "/ok")
    async def ok_head() -> dict:
        return {"ok": True}

    @route("get", "/auth", auth=[JWTAuthentication(secret="s" * 32)], guards=[IsAuthenticated()])
    async def auth() -> dict:
        return {}

    @route("get", "/limited")
    @rate_limit(rps=1, burst=1)
    async def limited() -> dict:
        return {}

    @route("get", "/items/{item_id}")
    async def item(item_id: int) -> dict:
        return {"id": item_id}

    @route("get", "/boom")
    async def boom() -> dict:
        raise RuntimeError("boom")

    @route("get", "/redirect")
    async def redirect():
        return Redirect("/ok")

    @route("get", "/stream")
    async def stream():
        return StreamingResponse(iter([b"a"]), media_type="text/plain")

    @route("get", "/file")
    async def file():
        return FileResponse(str(file_path))

    @route("get", "/vary-cookie")
    async def vary_cookie():
        return Response({}, headers={"Vary": "Cookie"})

    @route("get", "/vary-origin")
    async def vary_origin():
        return Response({}, headers={"Vary": "accept-encoding, ORIGIN"})

    @route("get", "/vary-star")
    async def vary_star():
        return Response({}, headers={"Vary": "*"})

    @route("get", "/big")
    async def big() -> dict:
        return {"data": "x" * 4000}

    api.mount_asgi("/asgi", _asgi_app)
    api.mount_django("/django", app=_asgi_app)
    return api


@pytest.fixture(scope="module", params=list(CONFIGS))
def configured(request, tmp_path_factory):
    """A client of the API under one CORS config, and the config."""
    cors_settings, route_decorator, mode = CONFIGS[request.param]
    static_dir = tmp_path_factory.mktemp("static")
    (static_dir / "a.css").write_text("body {}")
    api = _api(route_decorator, static_dir / "a.css")
    with (
        override_settings(**cors_settings),
        TestClient(
            api,
            raise_server_exceptions=False,
            follow_redirects=False,
            static_files_config={"url_prefix": "/static", "directories": [str(static_dir)]},
        ) as client,
    ):
        yield client, cors_settings, route_decorator, mode


def _vary_tokens(response) -> list[str]:
    return [token.strip().lower() for value in response.headers.get_list("vary") for token in value.split(",")]


@pytest.mark.parametrize("origin", list(ORIGINS))
@pytest.mark.parametrize("kind", list(RESPONSES))
def test_vary_origin_is_on_each_response_that_depends_on_the_origin(configured, kind, origin):
    client, cors_settings, route_decorator, mode = configured
    method, path, extra_headers, status, answered_by_route = RESPONSES[kind]
    request_origin = ORIGINS[origin]
    headers = dict(extra_headers)
    if request_origin is not None:
        headers["Origin"] = request_origin

    if not answered_by_route and route_decorator is not None:
        # The route config does not apply. The global config applies, if there is one.
        mode = "list" if cors_settings else "off"
    if path == "/limited":
        client.request(method, path, headers=headers)
    response = client.request(method, path, headers=headers)

    if status is None:
        # With no global config, Bolt does not answer a preflight to a path with no route.
        status = 204 if cors_settings else 404
    assert response.status_code == status
    allow_origin = response.headers.get("access-control-allow-origin")
    vary = _vary_tokens(response)

    if kind == "handler vary star":
        # `Vary: *` already names each request header.
        assert vary == ["*"]
    elif mode in ("list", "echo") or kind == "handler vary origin":
        assert vary.count("origin") == 1
    else:
        assert "origin" not in vary

    if mode == "wildcard":
        # One static value for each request needs no `Vary: Origin`.
        assert allow_origin == "*"
    elif request_origin == ALLOWED and mode in ("list", "echo") or request_origin == REFUSED and mode == "echo":
        assert allow_origin == request_origin
    else:
        assert allow_origin is None

    is_preflight = method == "OPTIONS" and request_origin is not None and mode != "off"
    for name in ("access-control-request-method", "access-control-request-headers"):
        assert vary.count(name) == (1 if is_preflight else 0)
    if kind == "handler vary cookie":
        assert "cookie" in vary
    if kind in ("handler vary origin", "compressed"):
        assert vary.count("accept-encoding") == 1


@pytest.mark.parametrize("origin", [None, ALLOWED])
def test_an_api_with_no_cors_config_gets_no_vary_origin(origin):
    api = BoltAPI()

    @api.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    headers = {"Origin": origin} if origin else {}
    with TestClient(api, read_django_settings=False) as client:
        response = client.get("/ok", headers=headers)
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers
        assert "vary" not in response.headers
