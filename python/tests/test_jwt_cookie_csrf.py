"""CSRF protection for cookie-authenticated JWT routes.

Bolt bypasses Django's ``CsrfViewMiddleware``, so a JWT read from a cookie —
which the browser attaches automatically — has no CSRF protection unless the
framework adds it. When a JWT backend sources its token from a cookie, an
unsafe-method request must prove it did not originate cross-site (via
``Sec-Fetch-Site``, falling back to an ``Origin``/``Referer`` host check).
Safe methods and header-sourced tokens are never subject to the check.
"""

from __future__ import annotations

import time

import jwt
import pytest

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication, TokenPair, set_token_cookies
from django_bolt.responses import PlainText
from django_bolt.testing import TestClient

SECRET = "cookie-csrf-secret"


def make_token() -> str:
    now = int(time.time())
    return jwt.encode({"sub": "u", "iat": now, "exp": now + 3600}, SECRET, algorithm="HS256")


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.post(
        "/cookie-write",
        auth=[JWTAuthentication(secret=SECRET, cookie="access_token")],
        guards=[IsAuthenticated()],
    )
    async def cookie_write(request: dict):
        return {"ok": True}

    @api.get(
        "/cookie-read",
        auth=[JWTAuthentication(secret=SECRET, cookie="access_token")],
        guards=[IsAuthenticated()],
    )
    async def cookie_read(request: dict):
        return {"ok": True}

    @api.post(
        "/cookie-write-nocsrf",
        auth=[JWTAuthentication(secret=SECRET, cookie="access_token", csrf=False)],
        guards=[IsAuthenticated()],
    )
    async def cookie_write_nocsrf(request: dict):
        return {"ok": True}

    @api.post(
        "/header-write",
        auth=[JWTAuthentication(secret=SECRET)],
        guards=[IsAuthenticated()],
    )
    async def header_write(request: dict):
        return {"ok": True}

    @api.post(
        "/mixed-write",
        auth=[
            JWTAuthentication(secret=SECRET, cookie="access_token"),
            JWTAuthentication(secret=SECRET),
        ],
        guards=[IsAuthenticated()],
    )
    async def mixed_write(request: dict):
        return {"ok": True}

    with TestClient(api) as c:
        yield c


def cookie_hdr(extra=None):
    headers = {"Cookie": f"access_token={make_token()}"}
    if extra:
        headers.update(extra)
    return headers


class TestCookieCsrf:
    def test_same_origin_post_allowed(self, client):
        r = client.request("POST", "/cookie-write", headers=cookie_hdr({"Sec-Fetch-Site": "same-origin"}))
        assert r.status_code == 200

    def test_cross_site_post_rejected(self, client):
        r = client.request("POST", "/cookie-write", headers=cookie_hdr({"Sec-Fetch-Site": "cross-site"}))
        assert r.status_code == 403

    def test_get_is_never_csrf_checked(self, client):
        # Safe method: no Sec-Fetch-Site needed, cross-site is fine.
        r = client.get("/cookie-read", headers=cookie_hdr({"Sec-Fetch-Site": "cross-site"}))
        assert r.status_code == 200

    def test_origin_fallback_same_host_allowed(self, client):
        r = client.request(
            "POST",
            "/cookie-write",
            headers=cookie_hdr({"Origin": "http://testserver", "Host": "testserver"}),
        )
        assert r.status_code == 200

    def test_origin_fallback_foreign_host_rejected(self, client):
        r = client.request(
            "POST",
            "/cookie-write",
            headers=cookie_hdr({"Origin": "http://evil.example", "Host": "testserver"}),
        )
        assert r.status_code == 403

    def test_unsafe_request_without_origin_signal_rejected(self, client):
        r = client.request("POST", "/cookie-write", headers=cookie_hdr())
        assert r.status_code == 403

    def test_csrf_disabled_allows_cross_site(self, client):
        r = client.request("POST", "/cookie-write-nocsrf", headers=cookie_hdr({"Sec-Fetch-Site": "cross-site"}))
        assert r.status_code == 200

    def test_header_token_route_not_csrf_checked(self, client):
        # Header-sourced tokens are not auto-attached by the browser, so CSRF
        # does not apply even on unsafe methods.
        r = client.request(
            "POST",
            "/header-write",
            headers={"Authorization": f"Bearer {make_token()}", "Sec-Fetch-Site": "cross-site"},
        )
        assert r.status_code == 200

    def test_cookieless_request_fails_auth_not_csrf(self, client):
        # No auth cookie on the request → nothing browser-attached for CSRF
        # to protect. The request simply fails authentication (401), it is
        # not rejected by the CSRF gate (403).
        r = client.request("POST", "/cookie-write", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 401

    def test_mixed_route_header_auth_without_cookie_allowed(self, client):
        # A route listing both a cookie backend and a header backend: a
        # request credentialed only via the header carries nothing the
        # browser auto-attached, so CSRF must not apply.
        r = client.request(
            "POST",
            "/mixed-write",
            headers={"Authorization": f"Bearer {make_token()}", "Sec-Fetch-Site": "cross-site"},
        )
        assert r.status_code == 200

    def test_mixed_route_with_cookie_still_csrf_checked(self, client):
        # A valid cookie backend appears first and therefore supplies the
        # accepted credential, so cookie CSRF still applies.
        r = client.request(
            "POST",
            "/mixed-write",
            headers=cookie_hdr({"Authorization": f"Bearer {make_token()}", "Sec-Fetch-Site": "cross-site"}),
        )
        assert r.status_code == 403

    def test_mixed_route_stale_cookie_does_not_block_header_auth(self, client):
        # CSRF follows the credential that actually authenticated. A stale
        # incidental cookie must not turn valid bearer authentication into a
        # pre-authentication 403.
        r = client.request(
            "POST",
            "/mixed-write",
            headers={
                "Cookie": "access_token=stale",
                "Authorization": f"Bearer {make_token()}",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert r.status_code == 200


class TestSetTokenCookies:
    def test_sets_httponly_secure_lax_cookies_with_scoped_refresh(self):
        now = int(time.time())
        pair = TokenPair(
            access_token="acc",
            refresh_token="ref",
            access_claims={"exp": now + 900},
            refresh_claims={"exp": now + 7200},
        )
        response = set_token_cookies(PlainText("ok"), pair, refresh_path="/auth/refresh")
        cookies = {c.name: c for c in response._cookies}

        assert set(cookies) == {"access_token", "refresh_token"}
        for cookie in cookies.values():
            assert cookie.httponly is True
            assert cookie.secure is True
            assert cookie.samesite == "Lax"
        # Access cookie is site-wide; refresh cookie is scoped to the rotation
        # endpoint so it never rides along on ordinary API calls.
        assert cookies["access_token"].path == "/"
        assert cookies["refresh_token"].path == "/auth/refresh"

    def test_omits_refresh_cookie_when_absent(self):
        now = int(time.time())
        pair = TokenPair(
            access_token="acc",
            refresh_token="",
            access_claims={"exp": now + 900},
            refresh_claims={},
        )
        response = set_token_cookies(PlainText("ok"), pair)
        names = {c.name for c in response._cookies}
        assert names == {"access_token"}
