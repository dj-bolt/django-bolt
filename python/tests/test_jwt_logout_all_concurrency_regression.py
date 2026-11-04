"""JWTAuthViews global logout must not invalidate a newer login.

This exercises the real registered logout handler with controlled async
interleavings; no Rust HTTP server or database is required for the race.
"""

from __future__ import annotations

import asyncio

import pytest

from django_bolt.auth import JWTAuthViews, create_token_pair
from django_bolt.auth.revocation import InMemoryRevocation
from django_bolt.exceptions import HTTPException

pytestmark = pytest.mark.django_db

SECRET = "jwt-atomic-logout-regression-secret"


class _Routes:
    def __init__(self):
        self.handlers = {}

    def post(self, path, **_options):
        def decorate(handler):
            self.handlers[path] = handler
            return handler

        return decorate


def _logout_handler(store):
    routes = _Routes()
    JWTAuthViews(store=store, secret=SECRET).register(routes)
    return routes.handlers["/auth/logout-all"]


def _request(pair, user_id="race-user"):
    return {"context": {"auth_claims": pair.refresh_claims, "user_id": user_id}}


class _CoordinatedStore(InMemoryRevocation):
    """Both requests validate v0; delay the second CAS until after new login."""

    def __init__(self):
        super().__init__()
        self.read_count = 0
        self.cas_count = 0
        self.both_read = asyncio.Event()
        self.release_reads = asyncio.Event()
        self.second_cas_waiting = asyncio.Event()
        self.release_second_cas = asyncio.Event()

    async def get_user_version(self, user_id):
        """Hold the first version reads to force a concurrent snapshot."""
        snapshot = await super().get_user_version(user_id)
        if self.read_count < 2:
            self.read_count += 1
            if self.read_count == 2:
                self.both_read.set()
            await self.release_reads.wait()
        return snapshot

    async def bump_user_version_if_current(self, user_id, expected_version):
        """Delay the second version update to expose a stale-request race."""
        self.cas_count += 1
        if self.cas_count == 2:
            self.second_cas_waiting.set()
            await self.release_second_cas.wait()
        return await super().bump_user_version_if_current(user_id, expected_version)


@pytest.mark.asyncio
async def test_old_concurrent_logout_cannot_invalidate_a_new_login():
    store = _CoordinatedStore()
    handler = _logout_handler(store)
    original = create_token_pair("race-user", secret=SECRET, version=0)

    first = asyncio.create_task(handler(_request(original)))
    second = asyncio.create_task(handler(_request(original)))
    try:
        await asyncio.wait_for(store.both_read.wait(), timeout=5)
        store.release_reads.set()
        await asyncio.wait_for(store.second_cas_waiting.wait(), timeout=5)

        # One logout completed, but the second request already validated v0.
        assert (await asyncio.wait_for(first, timeout=5)).status_code == 200
        new_version = await store.get_user_version("race-user")
        assert new_version == 1
        fresh = create_token_pair("race-user", secret=SECRET, version=new_version)

        store.release_second_cas.set()
        with pytest.raises(HTTPException) as caught:
            await asyncio.wait_for(second, timeout=5)
        assert caught.value.status_code == 401
        assert await store.get_user_version("race-user") == fresh.refresh_claims["ver"] == 1
    finally:
        store.release_reads.set()
        store.release_second_cas.set()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_two_distinct_valid_tokens_only_one_can_logout_all():
    """The protection is per user version, not merely duplicate JTI detection."""
    store = InMemoryRevocation()
    handler = _logout_handler(store)
    first = create_token_pair("shared-user", secret=SECRET, version=0)
    second = create_token_pair("shared-user", secret=SECRET, version=0)
    assert first.refresh_claims["jti"] != second.refresh_claims["jti"]

    outcomes = await asyncio.gather(
        handler(_request(first, "shared-user")),
        handler(_request(second, "shared-user")),
        return_exceptions=True,
    )
    assert sum(getattr(value, "status_code", 0) == 200 for value in outcomes) == 1
    rejected = [value for value in outcomes if isinstance(value, HTTPException)]
    assert len(rejected) == 1
    assert rejected[0].status_code == 401
    assert await store.get_user_version("shared-user") == 1


@pytest.mark.asyncio
async def test_atomic_compare_and_bump_is_single_use():
    store = InMemoryRevocation()
    assert await store.bump_user_version_if_current("user", 0) is True
    assert await store.bump_user_version_if_current("user", 0) is False
    assert await store.bump_user_version_if_current("user", 1) is True
    assert await store.get_user_version("user") == 2
