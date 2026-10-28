"""The origins of a route-level ``@cors`` are the complete allow list of the route.

A route with its own origins or origin regexes does not take the global
``CORS_ALLOWED_ORIGINS`` or ``CORS_ALLOWED_ORIGIN_REGEXES``.
"""

from __future__ import annotations

import re

import pytest

from django_bolt import BoltAPI, Router
from django_bolt.middleware import cors, middleware
from django_bolt.testing import TestClient

ALLOW_ORIGIN = "Access-Control-Allow-Origin"
ALLOW_CREDENTIALS = "Access-Control-Allow-Credentials"


@pytest.fixture
def global_cors(settings):
    settings.CORS_ALLOWED_ORIGINS = ["https://global.test"]
    settings.CORS_ALLOWED_ORIGIN_REGEXES = [r"^https://[a-z]+\.example\.com$"]


def _origin_headers(client: TestClient, path: str, origin: str) -> tuple[str | None, str | None]:
    response = client.get(path, headers={"Origin": origin})
    assert response.status_code == 200
    return response.headers.get(ALLOW_ORIGIN), response.headers.get(ALLOW_CREDENTIALS)


def test_route_origins_do_not_take_the_global_patterns(global_cors):
    api = BoltAPI()

    @api.get("/admin")
    @cors(origins=["https://admin.example.com"], credentials=True)
    async def admin():
        return {}

    @api.get("/open")
    async def open_route():
        return {}

    with TestClient(api, use_http_layer=True) as client:
        assert _origin_headers(client, "/admin", "https://admin.example.com") == ("https://admin.example.com", "true")
        # The global pattern matches this origin, but the route lists only its own.
        assert _origin_headers(client, "/admin", "https://shop.example.com") == (None, None)
        assert _origin_headers(client, "/admin", "https://global.test") == (None, None)
        # A route with no @cors keeps the global config.
        assert _origin_headers(client, "/open", "https://shop.example.com")[0] == "https://shop.example.com"


def test_route_origin_regexes_allow_matching_origins(global_cors):
    api = BoltAPI()

    @api.get("/tenant")
    @cors(origins=["https://app.test"], origin_regexes=[r"^https://[a-z]+\.tenant\.test$"])
    async def tenant():
        return {}

    with TestClient(api, use_http_layer=True) as client:
        assert _origin_headers(client, "/tenant", "https://app.test")[0] == "https://app.test"
        assert _origin_headers(client, "/tenant", "https://acme.tenant.test")[0] == "https://acme.tenant.test"
        assert _origin_headers(client, "/tenant", "https://acme.tenant.test.evil")[0] is None
        assert _origin_headers(client, "/tenant", "https://shop.example.com")[0] is None

        preflight = client.options(
            "/tenant",
            headers={"Origin": "https://acme.tenant.test", "Access-Control-Request-Method": "GET"},
        )
        assert preflight.headers.get(ALLOW_ORIGIN) == "https://acme.tenant.test"
        assert "GET" in preflight.headers.get("Access-Control-Allow-Methods", "")


def test_route_origin_regexes_without_origins(global_cors):
    api = BoltAPI()

    @api.get("/tenant")
    @cors(origin_regexes=[r"^https://[a-z]+\.tenant\.test$"])
    async def tenant():
        return {}

    with TestClient(api, use_http_layer=True) as client:
        assert _origin_headers(client, "/tenant", "https://acme.tenant.test")[0] == "https://acme.tenant.test"
        assert _origin_headers(client, "/tenant", "https://global.test")[0] is None


def test_route_origin_regexes_without_global_cors():
    api = BoltAPI()

    @api.get("/tenant")
    @cors(origin_regexes=[r"^https://[a-z]+\.tenant\.test$"])
    async def tenant():
        return {}

    with TestClient(api, use_http_layer=True, read_django_settings=False) as client:
        assert _origin_headers(client, "/tenant", "https://acme.tenant.test")[0] == "https://acme.tenant.test"
        assert _origin_headers(client, "/tenant", "https://other.test")[0] is None


def test_a_string_origin_regexes_is_one_pattern(global_cors):
    api = BoltAPI()

    @api.get("/tenant")
    @cors(origin_regexes=r"^https://[a-z]+\.tenant\.test$", credentials=True)
    async def tenant():
        return {}

    with TestClient(api, use_http_layer=True) as client:
        assert _origin_headers(client, "/tenant", "https://acme.tenant.test")[0] == "https://acme.tenant.test"
        assert _origin_headers(client, "/tenant", "https://attacker.evil") == (None, None)


def test_route_with_only_invalid_patterns_does_not_take_the_global_list(global_cors):
    api = BoltAPI()

    # The Rust regex crate has no look-around, so this pattern does not compile.
    @api.get("/tenant")
    @cors(origin_regexes=[r"^https://(?!evil)[a-z]+\.tenant\.test$"], credentials=True)
    async def tenant():
        return {}

    with TestClient(api, use_http_layer=True) as client:
        assert _origin_headers(client, "/tenant", "https://global.test") == (None, None)
        assert _origin_headers(client, "/tenant", "https://shop.example.com") == (None, None)


def test_route_cors_wins_over_the_cors_of_the_api():
    api = BoltAPI(middleware=[{"type": "cors", "origins": ["https://api.test"]}])

    @api.get("/route")
    @cors(origins=["https://route.test"])
    async def route():
        return {}

    @api.get("/plain")
    async def plain():
        return {}

    with TestClient(api, use_http_layer=True, read_django_settings=False) as client:
        assert _origin_headers(client, "/route", "https://route.test")[0] == "https://route.test"
        assert _origin_headers(client, "/route", "https://api.test")[0] is None
        assert _origin_headers(client, "/plain", "https://api.test")[0] == "https://api.test"


def test_the_most_specific_cors_config_wins():
    api = BoltAPI(middleware=[{"type": "cors", "origins": ["https://api.test"]}])
    router = Router(middleware=[{"type": "cors", "origins": ["https://router.test"]}])

    @router.get("/router-only")
    async def router_only():
        return {}

    @router.get("/route")
    @cors(origins=["https://route.test"])
    async def route():
        return {}

    api.include_router(router)

    with TestClient(api, use_http_layer=True, read_django_settings=False) as client:
        assert _origin_headers(client, "/router-only", "https://router.test")[0] == "https://router.test"
        assert _origin_headers(client, "/router-only", "https://api.test")[0] is None
        assert _origin_headers(client, "/route", "https://route.test")[0] == "https://route.test"
        assert _origin_headers(client, "/route", "https://router.test")[0] is None


def test_a_cors_config_with_no_origins_keeps_the_allow_list_of_the_outer_config(global_cors):
    # The router config sets only credentials. It must not replace the API list
    # with the global settings, which would give them credentialed CORS.
    api = BoltAPI(middleware=[{"type": "cors", "origins": ["https://api.test"]}])
    router = Router(middleware=[{"type": "cors", "credentials": True, "max_age": 10}])

    @router.get("/data")
    async def data():
        return {}

    api.include_router(router)

    with TestClient(api, use_http_layer=True) as client:
        assert _origin_headers(client, "/data", "https://api.test") == ("https://api.test", "true")
        assert _origin_headers(client, "/data", "https://global.test") == (None, None)
        assert _origin_headers(client, "/data", "https://shop.example.com") == (None, None)


@pytest.mark.parametrize("origins", ["https://a.test", {"https://a.test"}, ("https://a.test",)])
def test_a_cors_config_takes_a_str_or_a_collection_of_str(origins):
    # @cors takes the same forms.
    api = BoltAPI(middleware=[{"type": "cors", "origins": origins}])

    @api.get("/route")
    async def route():
        return {}

    with TestClient(api, use_http_layer=True, read_django_settings=False) as client:
        assert _origin_headers(client, "/route", "https://a.test")[0] == "https://a.test"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"origin_regexes": [re.compile(r"^https://[a-z]+\.tenant\.test$")]},
        {"origin_regexes": [b"^https://a\\.test$"]},
        {"origins": [None]},
        {"origins": ["https://a.test", 1]},
        {"origins": b"https://a.test"},
    ],
)
def test_cors_rejects_an_origin_or_a_pattern_that_is_not_a_str(kwargs):
    with pytest.raises(TypeError, match="str"):
        cors(credentials=True, **kwargs)(lambda: None)


def test_a_cors_config_that_is_not_a_list_of_str_stops_startup():
    api = BoltAPI(middleware=[{"type": "cors", "origins": ["https://a.test", 1], "credentials": True}])

    @api.get("/route")
    async def route():
        return {}

    with pytest.raises(TypeError, match="origins"), TestClient(api, read_django_settings=False):
        pass


def test_a_route_takes_one_cors_decorator():
    with pytest.raises(ValueError, match="already has a @cors"):

        @cors(origins=["https://outer.test"])
        @cors(origins=["https://inner.test"])
        async def route():
            return {}


@pytest.mark.parametrize("outer", ["cors", "middleware"])
def test_a_route_takes_one_cors_config_from_either_decorator(outer):
    async def route():
        return {}

    inner_decorator, outer_decorator = (
        cors(origins=["https://a.test"]),
        middleware(type="cors", origins=["https://b.test"]),
    )
    if outer == "cors":
        inner_decorator, outer_decorator = outer_decorator, inner_decorator
    with pytest.raises(ValueError, match="already has a @cors"):
        outer_decorator(inner_decorator(route))


def test_a_route_takes_one_positional_cors_dict():
    async def route():
        return {}

    first = middleware({"type": "cors", "origins": ["https://a.test"]})(route)
    with pytest.raises(ValueError, match="already has a @cors"):
        middleware({"type": "cors", "origins": ["https://b.test"]})(first)


def test_a_function_with_the_cors_of_another_route_gets_a_clear_error():
    # The cors config stays on the function, so a second route cannot give it other origins.
    api = BoltAPI()

    async def shared():
        return {}

    api.get("/a")(cors(origins=["https://a.test"])(shared))
    with pytest.raises(ValueError, match="give each route its own function"):
        cors(origins=["https://b.test"])(shared)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"origins": []}, {"origin_regexes": []}, {"origins": [], "origin_regexes": []}],
)
def test_cors_needs_an_origin_or_a_pattern(kwargs):
    with pytest.raises(ValueError, match="requires an 'origins' or 'origin_regexes' argument"):
        cors(**kwargs)(lambda: None)


@pytest.mark.parametrize("origin_regexes", ["", ["^https://a\\.test$", ""]])
def test_cors_rejects_an_empty_pattern(origin_regexes):
    # An empty pattern matches every origin.
    with pytest.raises(ValueError, match="empty pattern"):
        cors(origin_regexes=origin_regexes, credentials=True)(lambda: None)


def test_a_cors_config_with_an_empty_pattern_stops_startup():
    api = BoltAPI(middleware=[{"type": "cors", "origin_regexes": [""], "credentials": True}])

    @api.get("/route")
    async def route():
        return {}

    with pytest.raises(ValueError, match="empty pattern"), TestClient(api, read_django_settings=False):
        pass
