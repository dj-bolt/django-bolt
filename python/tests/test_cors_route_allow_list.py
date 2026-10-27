"""The origins of a route-level ``@cors`` are the complete allow list of the route.

A route with its own origins or origin regexes does not take the global
``CORS_ALLOWED_ORIGINS`` or ``CORS_ALLOWED_ORIGIN_REGEXES``.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI
from django_bolt.middleware import cors
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


def test_cors_needs_origins_or_origin_regexes():
    with pytest.raises(ValueError, match="requires 'origins'"):
        cors()(lambda: None)
