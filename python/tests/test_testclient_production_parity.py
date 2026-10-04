"""TestClient reads Django settings as `runbolt` reads them.

Each test sets a setting and expects the answer that a real `runbolt` server
gives for the same request. The production values were measured against
`runbolt`. Before the test client and the server shared one settings reader,
each of these answers differed.
"""

from __future__ import annotations

from django.test import override_settings

from django_bolt import BoltAPI
from django_bolt.middleware import rate_limit
from django_bolt.testing import TestClient


def _api() -> BoltAPI:
    api = BoltAPI()

    @api.post("/echo-len")
    async def echo_len(request):
        return {"len": len(request.body)}

    @api.get("/headers")
    async def headers(request):
        return {"count": len(request.headers)}

    @api.get("/plain")
    async def plain():
        return {"ok": True}

    @api.get("/remote")
    async def remote(request):
        return {"remote": request.META["REMOTE_ADDR"]}

    return api


def test_upload_limit_defaults_to_one_megabyte():
    with TestClient(_api()) as client:
        response = client.post("/echo-len", content=b"x" * (2 * 1024 * 1024))
    assert response.status_code == 413


def test_max_header_size_setting_applies():
    with override_settings(BOLT_MAX_HEADER_SIZE=200), TestClient(_api()) as client:
        response = client.get("/headers", headers={"X-Big": "y" * 300})
    assert response.status_code == 400


def test_cors_preflight_uses_the_server_defaults():
    with override_settings(CORS_ALLOWED_ORIGINS=["https://a.example"]), TestClient(_api()) as client:
        response = client.options(
            "/plain", headers={"Origin": "https://a.example", "Access-Control-Request-Method": "GET"}
        )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "https://a.example"
    assert response.headers["access-control-allow-headers"] == "Content-Type, Authorization"
    assert response.headers["access-control-max-age"] == "3600"


def test_cors_origin_regexes_alone_allow_a_matching_origin():
    with (
        override_settings(CORS_ALLOWED_ORIGIN_REGEXES=[r"^https://.*\.example$"]),
        TestClient(_api()) as client,
    ):
        response = client.get("/plain", headers={"Origin": "https://b.example"})
    assert response.headers["access-control-allow-origin"] == "https://b.example"


def test_a_star_in_cors_allowed_origins_is_not_a_wildcard():
    """Only CORS_ALLOW_ALL_ORIGINS allows every origin."""
    with override_settings(CORS_ALLOWED_ORIGINS=["*"]), TestClient(_api()) as client:
        response = client.get("/plain", headers={"Origin": "https://evil.test"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_empty_cors_origins_do_not_answer_a_preflight_for_an_unknown_route():
    with override_settings(CORS_ALLOWED_ORIGINS=[]), TestClient(_api()) as client:
        response = client.options(
            "/nope", headers={"Origin": "https://a.example", "Access-Control-Request-Method": "GET"}
        )
    assert response.status_code == 404


def test_static_max_age_sets_cache_control(tmp_path):
    (tmp_path / "app.css").write_text("body {}\n")
    with (
        override_settings(STATIC_URL="/static/", STATICFILES_DIRS=[str(tmp_path)], BOLT_STATIC_MAX_AGE=60),
        TestClient(_api()) as client,
    ):
        response = client.get("/static/app.css")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60"


def test_media_files_are_served(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    with (
        override_settings(MEDIA_URL="/media/", MEDIA_ROOT=str(tmp_path), BOLT_MEDIA_MAX_AGE=30),
        TestClient(_api()) as client,
    ):
        response = client.get("/media/a.txt")
    assert response.status_code == 200
    assert response.text == "hello\n"
    assert response.headers["cache-control"] == "private, max-age=30"


def test_a_trusted_proxy_forwards_the_client_address():
    """The test client connects from 127.0.0.1, like a local client."""
    with override_settings(BOLT_TRUSTED_PROXIES=["127.0.0.1"]), TestClient(_api()) as client:
        response = client.get("/remote", headers={"X-Forwarded-For": "203.0.113.9"})
    assert response.json() == {"remote": "203.0.113.9"}


def test_an_untrusted_peer_is_the_client_address():
    with TestClient(_api()) as client:
        response = client.get("/remote", headers={"X-Forwarded-For": "203.0.113.9"})
    assert response.json() == {"remote": "127.0.0.1"}


def _limited_api() -> BoltAPI:
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=1)
    async def limited():
        return {"ok": True}

    return api


def test_a_new_client_starts_with_empty_rate_limit_buckets():
    """A new test client is a new server, so an earlier test cannot use up its limit."""
    with TestClient(_limited_api()) as client:
        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 429

    # The same handler id, limit and client address as above.
    with TestClient(_limited_api()) as client:
        assert client.get("/limited").status_code == 200
