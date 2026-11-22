"""TestClient reads Django settings as `runbolt` reads them.

Each case in ``tests.integration.parity_cases`` sets a setting, sends a
request, and states the answer. This file checks the answers of TestClient.
``test_testclient_parity_server_integration.py`` checks the same answers
from a real `runbolt` server.
"""

from __future__ import annotations

import asyncio

import pytest
from django.test import override_settings

from django_bolt import BoltAPI, Request, WebSocket
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated
from django_bolt.middleware import rate_limit
from django_bolt.openapi import OpenAPIConfig
from django_bolt.responses import Redirect
from django_bolt.testing import AsyncTestClient, TestClient, WebSocketTestClient
from tests.integration.apps.testclient_parity import api
from tests.integration.parity_cases import CASES, PREFLIGHT, ParityCase


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_testclient_answers_as_runbolt(case: ParityCase, tmp_path):
    settings = case.prepare(tmp_path)
    with override_settings(**settings), TestClient(api) as client:
        case.check(client.request(case.method, case.path, headers=case.headers, content=case.body))


def test_a_static_prefix_of_slash_does_not_shadow_the_routes(tmp_path):
    """runbolt refuses STATIC_URL "/": it would serve every path as a file."""
    static_files = {"url_prefix": "/", "directories": [str(tmp_path)]}
    with TestClient(api, static_files_config=static_files) as client:
        assert client.get("/plain").json() == {"ok": True}


def test_a_header_value_loses_its_outer_spaces_and_tabs():
    """An HTTP server removes the spaces and tabs around a header value (RFC 9110 section 5.5).

    httpx refuses to send such a value to a real server, so this case is not in ``parity_cases``.
    """
    local_api = BoltAPI()

    @local_api.get("/probe")
    async def probe(request: Request):
        return {"probe": request.headers["x-probe"]}

    @local_api.websocket("/ws/probe")
    async def ws_probe(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text(websocket.headers["x-probe"])

    headers = {"X-Probe": " \t a  b\t "}

    with TestClient(local_api) as client:
        assert client.get("/probe", headers=headers).json() == {"probe": "a  b"}

    async def talk() -> tuple[str, str]:
        async with AsyncTestClient(local_api) as client:
            response = await client.get("/probe", headers=headers)
        async with WebSocketTestClient(local_api, "/ws/probe", headers=headers) as websocket:
            return response.json()["probe"], await websocket.receive_text()

    assert asyncio.run(talk()) == ("a  b", "a  b")


def test_read_django_settings_false_ignores_cors_static_and_media(tmp_path):
    """read_django_settings=False turns off the CORS, static and media settings."""
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "app.css").write_text("body {}\n")
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "a.txt").write_text("hello\n")
    settings = {
        "CORS_ALLOWED_ORIGINS": ["https://a.example"],
        "STATIC_URL": "/static/",
        "STATICFILES_DIRS": [str(tmp_path / "static")],
        "MEDIA_URL": "/media/",
        "MEDIA_ROOT": str(tmp_path / "media"),
    }
    with override_settings(**settings):
        with TestClient(api) as client:
            assert client.get("/static/app.css").status_code == 200
            assert client.get("/media/a.txt").status_code == 200
        with TestClient(api, read_django_settings=False) as client:
            assert client.get("/static/app.css").status_code == 404
            assert client.get("/media/a.txt").status_code == 404
            response = client.get("/plain", headers={"Origin": "https://a.example"})
            assert "access-control-allow-origin" not in response.headers


def test_cors_allowed_origins_replaces_the_cors_settings():
    """An explicit origin list replaces the CORS settings. The other values are the server defaults."""
    settings = {
        "CORS_ALLOWED_ORIGIN_REGEXES": [r"^https://.*\.example$"],
        "CORS_ALLOW_CREDENTIALS": True,
        "CORS_PREFLIGHT_MAX_AGE": 86400,
    }
    with override_settings(**settings), TestClient(api, cors_allowed_origins=["https://a.example"]) as client:
        response = client.get("/plain", headers={"Origin": "https://b.example"})
        assert "access-control-allow-origin" not in response.headers

        preflight = client.options("/plain", headers=PREFLIGHT)
        assert preflight.headers["access-control-allow-origin"] == "https://a.example"
        assert "access-control-allow-credentials" not in preflight.headers
        assert preflight.headers["access-control-allow-headers"] == "Content-Type, Authorization"
        assert preflight.headers["access-control-max-age"] == "3600"


def test_a_star_in_cors_allowed_origins_allows_each_origin():
    """Unlike the setting, a "*" in the explicit origin list is a wildcard."""
    with TestClient(api, cors_allowed_origins=["*"]) as client:
        response = client.get("/plain", headers={"Origin": "https://evil.test"})
        assert response.headers["access-control-allow-origin"] == "*"


def test_static_files_config_replaces_the_static_settings(tmp_path):
    """An explicit static config replaces STATIC_URL and BOLT_STATIC_MAX_AGE."""
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "app.css").write_text("body {}\n")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.css").write_text("body {}\n")
    settings = {
        "STATIC_URL": "/static/",
        "STATICFILES_DIRS": [str(tmp_path / "static")],
        "BOLT_STATIC_MAX_AGE": 60,
    }
    static_files = {"url_prefix": "/assets", "directories": [str(tmp_path / "assets")]}
    with override_settings(**settings), TestClient(api, static_files_config=static_files) as client:
        response = client.get("/assets/app.css")
        assert response.status_code == 200
        assert "cache-control" not in response.headers
        assert client.get("/static/app.css").status_code == 404


def _limited_api() -> BoltAPI:
    limited_api = BoltAPI()

    @limited_api.get("/limited")
    @rate_limit(rps=1, burst=1)
    async def limited():
        return {"ok": True}

    return limited_api


def test_each_client_starts_with_empty_rate_limit_buckets():
    """A new TestClient starts with empty rate-limit buckets, as a new server does.

    The clients here have the same handler ids and the same client address.
    Before, the buckets of an API outlived its client, so the result of a test
    depended on the tests that ran before it.
    """
    first = _limited_api()
    with TestClient(first) as client:
        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 429

    with TestClient(first) as client:
        assert client.get("/limited").status_code == 200

    with TestClient(_limited_api()) as client:
        assert client.get("/limited").status_code == 200


def _redirect_api() -> BoltAPI:
    local_api = BoltAPI()

    @local_api.get("/old")
    async def old():
        return Redirect("/new")

    @local_api.get("/new")
    async def new():
        return {"page": "new"}

    return local_api


def test_follow_redirects_false_gives_the_redirect():
    """The ``follow_redirects`` of the caller replaces the default of the client (#915)."""
    local_api = _redirect_api()
    with TestClient(local_api, follow_redirects=False) as client:
        response = client.get("/old")
    assert response.status_code == 307
    assert response.headers["location"] == "/new"


def test_the_client_follows_redirects_by_default():
    with TestClient(_redirect_api()) as client:
        response = client.get("/old")
    assert response.status_code == 200
    assert response.json() == {"page": "new"}


def test_async_client_follow_redirects_false_gives_the_redirect():
    async def run():
        async with AsyncTestClient(_redirect_api(), follow_redirects=False) as client:
            return await client.get("/old")

    response = asyncio.run(run())
    assert response.status_code == 307
    assert response.headers["location"] == "/new"


def _docs_api(**kwargs) -> BoltAPI:
    local_api = BoltAPI(**kwargs)

    @local_api.get("/items")
    async def items():
        return []

    return local_api


def test_the_client_serves_the_docs_routes_as_runbolt_does():
    """runbolt registers the docs routes of the API. The client does too (#932)."""
    with TestClient(_docs_api()) as client:
        schema = client.get("/docs/openapi.json")
        ui = client.get("/docs")
    assert schema.status_code == 200
    assert "/items" in schema.json()["paths"]
    assert ui.status_code == 200
    assert ui.headers["content-type"].startswith("text/html")


def test_the_async_client_serves_the_docs_routes():
    async def run():
        async with AsyncTestClient(_docs_api()) as client:
            return await client.get("/docs/openapi.json")

    response = asyncio.run(run())
    assert response.status_code == 200
    assert "/items" in response.json()["paths"]


def test_disabled_docs_are_not_served():
    local_api = _docs_api(openapi_config=OpenAPIConfig(title="T", version="1", enabled=False))
    with TestClient(local_api) as client:
        assert client.get("/docs/openapi.json").status_code == 404


def test_the_docs_routes_use_the_auth_of_the_openapi_config():
    config = OpenAPIConfig(
        title="T",
        version="1",
        auth=[APIKeyAuthentication(api_keys={"secret"})],
        guards=[IsAuthenticated()],
    )
    with TestClient(_docs_api(openapi_config=config)) as client:
        assert client.get("/docs/openapi.json").status_code == 401
        assert client.get("/docs/openapi.json", headers={"x-api-key": "secret"}).status_code == 200


def test_many_clients_of_one_api_serve_the_docs():
    local_api = _docs_api()
    for _ in range(3):
        with TestClient(local_api) as client:
            assert client.get("/docs/openapi.json").status_code == 200


def test_a_route_at_a_docs_path_is_refused_as_runbolt_refuses_it():
    """runbolt stops with "GET /docs is already registered" for this API. The client raises the same error."""
    local_api = _docs_api()

    @local_api.get("/docs")
    async def own_docs():
        return {"own": True}

    with pytest.raises(ValueError):
        TestClient(local_api)


def test_a_route_at_a_docs_path_is_served_when_the_docs_are_disabled():
    local_api = _docs_api(openapi_config=OpenAPIConfig(title="T", version="1", enabled=False))

    @local_api.get("/docs")
    async def own_docs():
        return {"own": True}

    with TestClient(local_api) as client:
        assert client.get("/docs").json() == {"own": True}
