"""Regression tests for Django Bolt JWTAuthViews stateful logout validation.

These tests encode the expected security behavior. They fail against
unfixed upstream and should pass after the dedicated JWTAuthViews fix.
"""

from __future__ import annotations

import asyncio

import jwt
import pytest

from django_bolt import BoltAPI
from django_bolt.auth import InMemoryRevocation, JWTAuthViews
from django_bolt.testing import TestClient

SECRET = "a-long-test-secret-for-jwt-auth-views-regression"
CSRF_HEADERS = {"Sec-Fetch-Site": "none"}
LOGIN = {"username": "alice", "password": "correct"}

pytestmark = pytest.mark.django_db


class TestUser:
    pk = "security-regression-user"
    is_active = True


def make_api():
    """Build the auth API with an isolated in-memory revocation store."""
    store = InMemoryRevocation()
    api = BoltAPI()

    async def validate(request, credentials):
        """Accept only valid test credentials."""
        return TestUser() if credentials.username == "alice" and credentials.password == "correct" else None

    JWTAuthViews(
        store=store,
        secret=SECRET,
        secure_cookies=False,
        credential_validator=validate,
    ).register(api)
    return api, store


def test_rotated_out_refresh_token_cannot_logout_all():
    """Reject a consumed token before it can invalidate all sessions."""
    api, store = make_api()
    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        old_token = client.cookies.get("refresh_token")
        old_jti = jwt.decode(old_token, SECRET, algorithms=["HS256"])["jti"]
        assert client.post("/auth/refresh", headers=CSRF_HEADERS).status_code == 200
        assert client.cookies.get("refresh_token") != old_token

    # Rotation consumed the old JTI. A separate client intentionally presents
    # the revoked cookie instead of the fresh cookie jar.
    assert asyncio.run(store.is_revoked(old_jti)) is True
    assert asyncio.run(store.get_user_version(TestUser.pk)) == 0
    with TestClient(api) as stale_client:
        response = stale_client.post(
            "/auth/logout-all",
            headers={**CSRF_HEADERS, "Cookie": f"refresh_token={old_token}"},
        )
    assert response.status_code == 401, (
        f"Revoked refresh JWT was accepted for global logout: HTTP {response.status_code}: {response.text}"
    )
    assert asyncio.run(store.get_user_version(TestUser.pk)) == 0


def test_old_refresh_after_global_logout_cannot_invalidate_new_login():
    """Keep a new login active when an older token attempts logout-all."""
    api, store = make_api()
    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        old_token = client.cookies.get("refresh_token")
        assert client.post("/auth/logout-all", headers=CSRF_HEADERS).status_code == 200
        assert asyncio.run(store.get_user_version(TestUser.pk)) == 1
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        current_token = client.cookies.get("refresh_token")
        assert current_token and current_token != old_token

    # The older session is version=0; freshly logged-in session is version=1.
    old_claims = jwt.decode(old_token, SECRET, algorithms=["HS256"])
    current_claims = jwt.decode(current_token, SECRET, algorithms=["HS256"])
    assert old_claims["ver"] == 0
    assert current_claims["ver"] == 1

    with TestClient(api) as stale_client:
        response = stale_client.post(
            "/auth/logout-all",
            headers={**CSRF_HEADERS, "Cookie": f"refresh_token={old_token}"},
        )

    assert response.status_code == 401, (
        f"Stale refresh JWT was allowed to invalidate the newer login: HTTP {response.status_code}: {response.text}"
    )
    assert asyncio.run(store.get_user_version(TestUser.pk)) == 1


def test_rotated_out_refresh_token_cannot_logout_current_family():
    """An old, already-used token must not invalidate its valid descendant."""
    api, store = make_api()
    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        old_token = client.cookies.get("refresh_token")
        old_claims = jwt.decode(old_token, SECRET, algorithms=["HS256"])
        assert client.post("/auth/refresh", headers=CSRF_HEADERS).status_code == 200
        current_token = client.cookies.get("refresh_token")
        assert current_token and current_token != old_token

    with TestClient(api) as stale_client:
        response = stale_client.post(
            "/auth/logout",
            headers={**CSRF_HEADERS, "Cookie": f"refresh_token={old_token}"},
        )
    assert response.status_code == 401
    assert asyncio.run(store.is_family_revoked(old_claims["fam"])) is False
    # The newer, valid descendant still has access to refresh.
    with TestClient(api) as fresh_client:
        response = fresh_client.post(
            "/auth/refresh",
            headers={**CSRF_HEADERS, "Cookie": f"refresh_token={current_token}"},
        )
    assert response.status_code == 200


def test_revoked_family_cannot_logout_all():
    """A signed refresh JWT from an explicitly revoked family is inactive."""
    api, store = make_api()
    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        token = client.cookies.get("refresh_token")
        claims = jwt.decode(token, SECRET, algorithms=["HS256"])
        asyncio.run(store.revoke_family(claims["fam"]))
        response = client.post("/auth/logout-all", headers=CSRF_HEADERS)
        assert response.status_code == 401
        assert asyncio.run(store.get_user_version(TestUser.pk)) == 0


def test_refresh_reuse_still_revokes_family():
    """Rejecting old tokens during logout must not hide reuse from /refresh."""
    api, store = make_api()
    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        old_token = client.cookies.get("refresh_token")
        old_claims = jwt.decode(old_token, SECRET, algorithms=["HS256"])
        assert client.post("/auth/refresh", headers=CSRF_HEADERS).status_code == 200

    with TestClient(api) as stale_client:
        response = stale_client.post(
            "/auth/refresh",
            headers={**CSRF_HEADERS, "Cookie": f"refresh_token={old_token}"},
        )
    assert response.status_code == 401
    assert asyncio.run(store.is_family_revoked(old_claims["fam"])) is True
