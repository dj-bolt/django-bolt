"""Regression coverage for opt-in JWT access-token revocation policy."""

from __future__ import annotations

import asyncio

import jwt
import pytest

from django_bolt import BoltAPI
from django_bolt.auth import InMemoryRevocation, IsAuthenticated, JWTAuthentication, JWTAuthViews
from django_bolt.testing import TestClient

pytestmark = pytest.mark.django_db

SECRET = "jwt-access-revocation-policy-regression-secret"
LOGIN = {"username": "alice", "password": "correct"}
CSRF_HEADERS = {"Sec-Fetch-Site": "none"}


class _User:
    pk = "jwt-policy-user"
    is_active = True


class _RecordingStore(InMemoryRevocation):
    """Count version lookups to verify the stateless/default tradeoff."""

    def __init__(self):
        super().__init__()
        self.version_reads = 0

    async def get_user_version(self, user_id):
        self.version_reads += 1
        return await super().get_user_version(user_id)


def _strict_handler(store):
    """Application-defined opt-in policy; not a new framework default."""

    async def revoked(jti, claims):
        """Reject revoked JTIs, invalid claims, or stale user versions."""
        if await store.is_revoked(jti):
            return True
        user_id = claims.get("sub")
        version = claims.get("ver")
        if not isinstance(user_id, str) or not user_id or type(version) is not int:
            return True
        return version != await store.get_user_version(user_id)

    return revoked


def _api(store):
    """Register auth routes against the supplied test revocation store."""
    api = BoltAPI()

    async def validate(_request, credentials):
        """Accept only the test user's expected credentials."""
        return _User() if credentials.username == "alice" and credentials.password == "correct" else None

    JWTAuthViews(
        store=store,
        secret=SECRET,
        secure_cookies=False,
        credential_validator=validate,
    ).register(api)

    @api.get(
        "/default-access",
        auth=[JWTAuthentication(secret=SECRET, revocation_store=store)],
        guards=[IsAuthenticated()],
    )
    async def default_access(request):
        return {"user_id": request["context"]["user_id"]}

    @api.get(
        "/strict-access",
        auth=[JWTAuthentication(secret=SECRET, revoked_token_handler=_strict_handler(store))],
        guards=[IsAuthenticated()],
    )
    async def strict_access(request):
        return {"user_id": request["context"]["user_id"]}

    return api


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_access_tokens_remain_stateless_unless_app_opts_into_version_check():
    """JTI-only default survives logout-all; explicit version policy does not."""
    store = _RecordingStore()
    api = _api(store)
    with TestClient(api) as client:
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        old_access = client.cookies.get("access_token")
        assert old_access
        old_jti = jwt.decode(old_access, SECRET, algorithms=["HS256"])["jti"]

        assert client.get("/default-access", headers=_bearer(old_access)).status_code == 200
        assert client.get("/strict-access", headers=_bearer(old_access)).status_code == 200
        assert client.post("/auth/logout-all", headers=CSRF_HEADERS).status_code == 200

        # By design the stock helper only checks jti, not `ver`. It must
        # not add one storage lookup per request without application opt-in.
        store.version_reads = 0
        assert client.get("/default-access", headers=_bearer(old_access)).status_code == 200
        assert store.version_reads == 0

        assert client.get("/strict-access", headers=_bearer(old_access)).status_code == 401
        assert store.version_reads >= 1

        # New login gets the new version and may access the strict endpoint.
        assert client.post("/auth/login", json=LOGIN).status_code == 200
        new_access = client.cookies.get("access_token")
        assert new_access and new_access != old_access
        assert client.get("/strict-access", headers=_bearer(new_access)).status_code == 200

        # A token explicitly revoked by jti fails under either policy.
        asyncio.run(store.revoke(old_jti))
        assert client.get("/default-access", headers=_bearer(old_access)).status_code == 401
        assert client.get("/strict-access", headers=_bearer(old_access)).status_code == 401


@pytest.mark.asyncio
async def test_custom_strict_policy_rejects_missing_or_malformed_version():
    store = InMemoryRevocation()
    revoked = _strict_handler(store)
    assert await revoked("jti-1", {"sub": "user", "ver": 0}) is False
    assert await revoked("jti-1", {"sub": "user"}) is True
    assert await revoked("jti-1", {"sub": "user", "ver": "0"}) is True
    assert await revoked("jti-1", {"sub": "user", "ver": True}) is True
    assert await revoked("jti-1", {"sub": "", "ver": 0}) is True
    assert await store.bump_user_version_if_current("user", 0) is True
    assert await revoked("jti-1", {"sub": "user", "ver": 0}) is True
    assert await revoked("jti-2", {"sub": "user", "ver": 1}) is False
    await store.revoke("jti-2")
    assert await revoked("jti-2", {"sub": "user", "ver": 1}) is True
