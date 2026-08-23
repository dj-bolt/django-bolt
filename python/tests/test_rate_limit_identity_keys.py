"""
`@rate_limit(key="user")` and `key="api_key"` count per authenticated identity.

The check runs after authentication. A caller without an identity falls back
to one bucket per client address. Tokens are minted with the public
`create_jwt_for_user()` API. Refs #301.
"""

from __future__ import annotations

import itertools

import pytest
from django.contrib.auth import get_user_model

from django_bolt import BoltAPI
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication, create_jwt_for_user
from django_bolt.middleware import rate_limit
from django_bolt.testing import TestClient

SECRET = "rate-limit-identity-secret"
BURST = 3

# Limiters live in one process-wide map keyed by (handler id, quota, bucket).
# Every `BoltAPI` numbers handlers from 0, so a fresh app with the same quota
# would inherit the last test's buckets. A distinct `rps` per app keeps the
# same `BURST` and isolates the tests.
_rps = itertools.count(1)


def bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_jwt_for_user(user, secret=SECRET)}"}


def _make_api(key: str, auth, guards=()):
    api = BoltAPI()

    @api.get("/limited", auth=auth, guards=list(guards))
    @rate_limit(rps=next(_rps), burst=BURST, key=key)
    async def limited():
        return {"ok": True}

    return api


def _statuses(client, n: int, **kwargs) -> list[int]:
    return [client.get("/limited", **kwargs).status_code for _ in range(n)]


@pytest.fixture
def users(db):
    model = get_user_model()
    return [model.objects.create(username=f"u{i}") for i in range(3)]


@pytest.mark.django_db
def test_user_key_gives_each_user_its_own_bucket(users):
    with TestClient(_make_api("user", [JWTAuthentication(secret=SECRET)])) as client:
        for user in users:
            assert _statuses(client, BURST, headers=bearer(user)) == [200] * BURST
        assert client.get("/limited", headers=bearer(users[0])).status_code == 429


@pytest.mark.django_db
def test_user_key_still_limits_one_user(users):
    with TestClient(_make_api("user", [JWTAuthentication(secret=SECRET)])) as client:
        assert _statuses(client, BURST + 2, headers=bearer(users[0])) == [200] * BURST + [429, 429]


@pytest.mark.django_db
def test_user_key_unauthenticated_callers_share_an_ip_bucket(users):
    """No identity: limit per client address, and do not touch any user's bucket."""
    with TestClient(_make_api("user", [JWTAuthentication(secret=SECRET)])) as client:
        assert _statuses(client, BURST + 1) == [200] * BURST + [429]
        assert _statuses(client, BURST, headers=bearer(users[0])) == [200] * BURST


@pytest.mark.django_db
def test_user_key_with_guard_rejects_before_counting(users):
    """A 401 is not a spent token for the user that later authenticates."""
    api = _make_api("user", [JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    with TestClient(api) as client:
        assert _statuses(client, BURST + 1) == [401] * (BURST + 1)
        assert _statuses(client, BURST, headers=bearer(users[0])) == [200] * BURST


def test_api_key_gives_each_key_its_own_bucket():
    auth = [APIKeyAuthentication(api_keys={"key-a", "key-b"}, header="x-api-key")]
    with TestClient(_make_api("api_key", auth)) as client:
        assert _statuses(client, BURST, headers={"X-API-Key": "key-a"}) == [200] * BURST
        assert _statuses(client, BURST, headers={"X-API-Key": "key-b"}) == [200] * BURST
        assert client.get("/limited", headers={"X-API-Key": "key-a"}).status_code == 429


def test_api_key_unknown_key_falls_back_to_ip_bucket():
    """An invalid key has no identity; it cannot pick a fresh bucket per value."""
    auth = [APIKeyAuthentication(api_keys={"key-a"}, header="x-api-key")]
    with TestClient(_make_api("api_key", auth)) as client:
        statuses = [client.get("/limited", headers={"X-API-Key": f"bogus-{i}"}).status_code for i in range(BURST + 1)]
        assert statuses == [200] * BURST + [429]


@pytest.mark.django_db
def test_api_key_ignores_jwt_identity(users):
    """`key="api_key"` counts API keys only. A JWT caller is limited by address."""
    auth = [JWTAuthentication(secret=SECRET), APIKeyAuthentication(api_keys={"key-a"}, header="x-api-key")]
    with TestClient(_make_api("api_key", auth)) as client:
        statuses = [client.get("/limited", headers=bearer(u)).status_code for u in users] + [
            client.get("/limited", headers=bearer(users[0])).status_code
        ]
        assert statuses == [200] * BURST + [429]
        assert _statuses(client, BURST, headers={"X-API-Key": "key-a"}) == [200] * BURST


@pytest.mark.parametrize("key", ["user", "api_key"])
def test_identity_key_without_auth_fails_at_startup(key):
    """No backend can ever produce an identity, so the route must not start."""
    with pytest.raises(ValueError, match=f'key="{key}"'):
        TestClient(_make_api(key, None))
