"""Additional JWT lifecycle regressions for PR #562 (no external services required)."""

from __future__ import annotations

import asyncio

import jwt
import pytest

from django_bolt import BoltAPI
from django_bolt.auth import (
    InMemoryRevocation,
    JWTAuthViews,
    TokenRotationError,
    create_token_pair,
    rotate_refresh_token,
)
from django_bolt.testing import TestClient

pytestmark = pytest.mark.django_db

SECRET = "jwt-refresh-policy-tests-with-a-long-test-secret"
CSRF_HEADERS = {"Sec-Fetch-Site": "none"}
LOGIN = {"username": "alice", "password": "correct"}


class User:
    pk = "refresh-policy-user"
    is_active = True


def make_api():
    """Build test auth routes with an in-memory revocation store."""
    api = BoltAPI()
    store = InMemoryRevocation()

    async def validate(request, credentials):
        """Accept only the configured test credentials."""
        return User() if credentials.username == "alice" and credentials.password == "correct" else None

    JWTAuthViews(
        store=store,
        secret=SECRET,
        secure_cookies=False,
        credential_validator=validate,
    ).register(api)
    return api, store


def signed_without_version(token):
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    claims.pop("ver")
    return jwt.encode(claims, SECRET, algorithm="HS256")


@pytest.mark.parametrize("endpoint", ["/auth/refresh", "/auth/logout", "/auth/logout-all"])
def test_legacy_versionless_refresh_has_version_zero_semantics(endpoint):
    """Apply version-zero rules to legacy refresh tokens without ver."""
    api, store = make_api()
    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        legacy_token = signed_without_version(client.cookies.get("refresh_token"))

    with TestClient(api) as client:
        response = client.post(
            endpoint,
            headers={**CSRF_HEADERS, "Cookie": f"refresh_token={legacy_token}"},
        )
        assert response.status_code == 200, response.text

    if endpoint == "/auth/logout-all":
        # In the compatibility policy, a missing ver is equivalent to version 0.
        assert asyncio.run(store.get_user_version(User.pk)) == 1


@pytest.mark.parametrize("endpoint", ["/auth/refresh", "/auth/logout", "/auth/logout-all"])
def test_legacy_versionless_refresh_rejected_after_global_logout(endpoint):
    api, store = make_api()
    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        old_legacy_token = signed_without_version(client.cookies.get("refresh_token"))
        assert client.post("/auth/logout-all", headers=CSRF_HEADERS).status_code == 200

    assert asyncio.run(store.get_user_version(User.pk)) == 1

    with TestClient(api) as client:
        response = client.post(
            endpoint,
            headers={**CSRF_HEADERS, "Cookie": f"refresh_token={old_legacy_token}"},
        )
        assert response.status_code == 401, response.text
    assert asyncio.run(store.get_user_version(User.pk)) == 1


@pytest.mark.parametrize("bad_version", ["0", True, -1, 0.5])
@pytest.mark.parametrize("endpoint", ["/auth/refresh", "/auth/logout", "/auth/logout-all"])
def test_malformed_version_cannot_mutate_session_state(endpoint, bad_version):
    api, store = make_api()
    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        claims = jwt.decode(client.cookies.get("refresh_token"), SECRET, algorithms=["HS256"])

    claims["ver"] = bad_version
    bad_token = jwt.encode(claims, SECRET, algorithm="HS256")

    with TestClient(api) as client:
        response = client.post(
            endpoint,
            headers={**CSRF_HEADERS, "Cookie": f"refresh_token={bad_token}"},
        )
        assert response.status_code == 401, response.text

    assert asyncio.run(store.get_user_version(User.pk)) == 0
    assert asyncio.run(store.is_family_revoked(claims["fam"])) is False
    assert asyncio.run(store.is_revoked(claims["jti"])) is False


@pytest.mark.asyncio
async def test_optional_batch_fallback_respects_custom_family_policy():
    class CustomPolicyStore(InMemoryRevocation):
        def __init__(self):
            super().__init__()
            self.checked_family = False
            self.consumed = False

        async def get_refresh_state(self, jti, fam, user_id):
            return None  # Force the documented per-method fallback.

        async def is_family_revoked(self, fam):
            self.checked_family = True
            return True  # Additional policy rejects the family.

        async def consume(self, jti, *, exp=None):
            self.consumed = True
            return await super().consume(jti, exp=exp)

    store = CustomPolicyStore()
    pair = create_token_pair("user-1", secret=SECRET)

    with pytest.raises(TokenRotationError, match="family revoked"):
        await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)
    assert store.checked_family is True
    assert store.consumed is False


@pytest.mark.asyncio
async def test_batch_read_failure_fails_closed_without_consuming_token():
    class BrokenBatchStore(InMemoryRevocation):
        async def get_refresh_state(self, jti, fam, user_id):
            raise RuntimeError("Simulated primary Redis read failure")

    store = BrokenBatchStore()
    pair = create_token_pair("user-1", secret=SECRET)

    with pytest.raises(RuntimeError, match="Simulated primary Redis read failure"):
        await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)
    assert await store.is_revoked(pair.refresh_claims["jti"]) is False


@pytest.mark.parametrize(
    "endpoint",
    ["/auth/refresh", "/auth/logout", "/auth/logout-all"],
)
def test_null_version_cannot_bypass_global_logout(endpoint):
    api, _store = make_api()

    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        claims = jwt.decode(
            client.cookies.get("refresh_token"),
            SECRET,
            algorithms=["HS256"],
        )
        assert client.post("/auth/logout-all", headers=CSRF_HEADERS).status_code == 200

    claims["ver"] = None
    token = jwt.encode(claims, SECRET, algorithm="HS256")

    with TestClient(api) as client:
        response = client.post(
            endpoint,
            headers={
                **CSRF_HEADERS,
                "Cookie": f"refresh_token={token}",
            },
        )

    assert response.status_code == 401
