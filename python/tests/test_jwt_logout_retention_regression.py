"""JWT logout revocation retention and versioned-store contract regressions."""

from __future__ import annotations

import time

import pytest

from django_bolt.auth import JWTAuthViews, create_token_pair
from django_bolt.auth.revocation import InMemoryRevocation
from django_bolt.auth.tokens import DEFAULT_REFRESH_TTL
from django_bolt.exceptions import HTTPException

pytestmark = pytest.mark.django_db

SECRET = "a-long-test-secret-for-jwt-logout-retention-regression"


class _Routes:
    def __init__(self):
        self.handlers = {}

    def post(self, path, **_options):
        def decorate(handler):
            self.handlers[path] = handler
            return handler

        return decorate


def _handler(store, path):
    routes = _Routes()
    JWTAuthViews(store=store, secret=SECRET).register(routes)
    return routes.handlers[path]


class _RecordingStore(InMemoryRevocation):
    def __init__(self):
        super().__init__()
        self.last_family_exp = None

    async def revoke_family(self, fam, *, exp=None):
        self.last_family_exp = exp
        await super().revoke_family(fam, exp=exp)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_lifetime",
    [None, 30, 2 * DEFAULT_REFRESH_TTL],
    ids=["no-exp", "short-lived", "long-lived"],
)
async def test_logout_family_marker_covers_grace_and_inflight_refresh(token_lifetime):
    """Logout must block 60s JWT leeway AND any successor minted in flight."""
    store = _RecordingStore()
    handler = _handler(store, "/auth/logout")
    now = int(time.time())
    pair = create_token_pair("user", secret=SECRET)
    claims = dict(pair.refresh_claims)
    if token_lifetime is None:
        claims.pop("exp")
    else:
        claims["exp"] = now + token_lifetime

    response = await handler({"context": {"auth_claims": claims, "user_id": "user"}})

    assert response.status_code == 200
    # The implementation uses the 60-second leeway of JWTAuthentication.
    assert store.last_family_exp >= max(claims.get("exp", 0), now + DEFAULT_REFRESH_TTL) + 60
    assert await store.is_family_revoked(claims["fam"])


class _LegacyVersionStore(InMemoryRevocation):
    """A user-defined store that has no atomic version-update support."""

    async def bump_user_version_if_current(self, user_id, expected_version):
        raise NotImplementedError("No CAS in this backend")


@pytest.mark.asyncio
async def test_logout_all_legacy_store_fails_closed_without_bumping_version():
    store = _LegacyVersionStore()
    handler = _handler(store, "/auth/logout-all")
    pair = create_token_pair("user", secret=SECRET, version=0)
    request = {"context": {"auth_claims": pair.refresh_claims, "user_id": "user"}}

    with pytest.raises(HTTPException) as caught:
        await handler(request)

    assert caught.value.status_code == 503
    assert await store.get_user_version("user") == 0
