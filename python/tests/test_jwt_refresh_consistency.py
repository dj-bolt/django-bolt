"""JWT rotation safety and primary Redis batching regression tests (PR #562)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import django_bolt.auth.tokens as tokens
from django_bolt.auth import (
    InMemoryRevocation,
    JWTAuthViews,
    TokenRotationError,
    create_token_pair,
    rotate_refresh_token,
)
from django_bolt.exceptions import HTTPException

SECRET = "jwt-refresh-performance-regression-secret"


@pytest.mark.asyncio
async def test_concurrent_logout_marker_outlives_inflight_refresh(monkeypatch):

    clock = [1_800_000_000]
    monkeypatch.setattr(tokens.time, "time", lambda: clock[0])
    ttl = 120
    leeway = 60
    original = create_token_pair("user-1", secret=SECRET, refresh_ttl=ttl)

    class PausedStore(InMemoryRevocation):
        def __init__(self):
            super().__init__()
            self.read_complete = asyncio.Event()
            self.proceed = asyncio.Event()

        async def is_family_revoked(self, fam):
            old_value = await super().is_family_revoked(fam)
            self.read_complete.set()
            await self.proceed.wait()
            return old_value

    store = PausedStore()
    task = asyncio.create_task(
        rotate_refresh_token(original.refresh_claims, store=store, secret=SECRET, refresh_ttl=ttl, leeway=leeway)
    )
    await asyncio.wait_for(store.read_complete.wait(), timeout=3)
    clock[0] += 3
    logout_marker_end = clock[0] + ttl + leeway
    await store.revoke_family(original.refresh_claims["fam"], exp=logout_marker_end)
    store.proceed.set()
    rotated = await task
    assert rotated.refresh_claims["exp"] + leeway <= logout_marker_end
    assert rotated.refresh_claims["iat"] == clock[0]


class BatchStore(InMemoryRevocation):
    def __init__(self):
        super().__init__()
        self.batch_calls = 0
        self.revoked_jti = False
        self.consume_calls = 0

    async def get_refresh_state(self, jti, fam, user_id):
        self.batch_calls += 1
        return self.revoked_jti, False, 0

    async def is_family_revoked(self, fam):
        raise AssertionError("Must not perform a second family read")

    async def get_user_version(self, user_id):
        raise AssertionError("Must not perform a second version read")

    async def consume(self, jti, *, exp=None):
        self.consume_calls += 1
        return await super().consume(jti, exp=exp)


@pytest.mark.asyncio
async def test_rotation_uses_one_batch_and_still_consumes_atomically():
    store = BatchStore()
    pair = create_token_pair("user-1", secret=SECRET)
    rotated = await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)
    assert store.batch_calls == 1
    assert store.consume_calls == 1
    assert rotated.refresh_claims["fam"] == pair.refresh_claims["fam"]


@pytest.mark.asyncio
async def test_batch_jti_does_not_skip_replay_detection():
    store = BatchStore()
    pair = create_token_pair("user-1", secret=SECRET)
    await store.consume(pair.refresh_claims["jti"])
    store.revoked_jti = True
    with pytest.raises(TokenRotationError, match="revoked or already used"):
        await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)
    assert store.batch_calls == 1
    assert store.consume_calls == 2
    assert await InMemoryRevocation.is_family_revoked(store, pair.refresh_claims["fam"])


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "0", True, -1, 0.0])
async def test_refresh_rejects_invalid_version_without_store_reads(version):
    store = BatchStore()
    claims = dict(create_token_pair("user-1", secret=SECRET).refresh_claims)
    claims["ver"] = version
    with pytest.raises(TokenRotationError, match="invalid version"):
        await rotate_refresh_token(claims, store=store, secret=SECRET)
    assert store.batch_calls == 0


@pytest.mark.asyncio
async def test_legacy_store_without_batch_reader_still_rotates():
    store = InMemoryRevocation()
    pair = create_token_pair("user-1", secret=SECRET)
    rotated = await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)
    assert rotated.refresh_claims["fam"] == pair.refresh_claims["fam"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all"])
@pytest.mark.parametrize("version", [None, "0", True, -1])
async def test_logout_views_reject_invalid_versions(endpoint, version):
    store = InMemoryRevocation()
    pair = create_token_pair("user-1", secret=SECRET)

    class Routes:
        def __init__(self):
            self.handlers = {}

        def post(self, path, **kwargs):
            def register(fn):
                self.handlers[path] = fn
                return fn

            return register

    routes = Routes()
    JWTAuthViews(store=store, secret=SECRET).register(routes)
    claims = dict(pair.refresh_claims)
    claims["ver"] = version
    request = {"context": {"auth_claims": claims, "user_id": "user-1"}}
    with pytest.raises(HTTPException) as exc:
        await routes.handlers[endpoint](request)
    assert exc.value.status_code == 401
    assert await store.get_user_version("user-1") == 0
    assert not await store.is_family_revoked(claims["fam"])


@pytest.mark.asyncio
async def test_session_cap_remains_absolute_after_slow_store_read(monkeypatch):
    """Store latency must not move access expiry past the origin-session cap."""
    clock = [1_800_000_000]
    monkeypatch.setattr(tokens, "time", SimpleNamespace(time=lambda: clock[0]))
    pair = create_token_pair("slow-user", secret=SECRET, oat=clock[0] - 90, refresh_ttl=120)
    session_cap = pair.refresh_claims["oat"] + 100

    class SlowStore(InMemoryRevocation):
        async def get_user_version(self, user_id):
            clock[0] += 5
            return await super().get_user_version(user_id)

    rotated = await rotate_refresh_token(
        pair.refresh_claims,
        store=SlowStore(),
        secret=SECRET,
        access_ttl=900,
        refresh_ttl=120,
        max_session_lifetime=100,
    )
    assert rotated.access_claims["exp"] <= session_cap
    decoded = tokens.jwt.decode(
        rotated.access_token,
        SECRET,
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )
    assert decoded["exp"] == rotated.access_claims["exp"]
    assert decoded["exp"] <= session_cap


@pytest.mark.asyncio
async def test_expired_session_during_store_read_does_not_consume_refresh(monkeypatch):
    """Fail closed before consume() when the session expires during a slow read."""
    clock = [1_800_000_000]
    monkeypatch.setattr(tokens, "time", SimpleNamespace(time=lambda: clock[0]))
    pair = create_token_pair("expired-during-read", secret=SECRET)

    class SlowStore(InMemoryRevocation):
        async def get_user_version(self, user_id):
            clock[0] += 6
            return await super().get_user_version(user_id)

    store = SlowStore()
    with pytest.raises(TokenRotationError, match="maximum lifetime"):
        await rotate_refresh_token(
            pair.refresh_claims,
            store=store,
            secret=SECRET,
            max_session_lifetime=5,
        )
    assert not await store.is_revoked(pair.refresh_claims["jti"])


@pytest.mark.asyncio
async def test_reuse_marker_deadline_is_measured_after_delayed_failure(monkeypatch):
    """A late replay failure must cover successors from overlapping rotation."""
    clock = [1_800_000_000]
    monkeypatch.setattr(tokens, "time", SimpleNamespace(time=lambda: clock[0]))
    ttl = 120
    leeway = 60
    pair = create_token_pair("replay-user", secret=SECRET, refresh_ttl=ttl)

    class DelayedReplayStore(InMemoryRevocation):
        def __init__(self):
            super().__init__()
            self.family_exp = None

        async def consume(self, jti, *, exp=None):
            clock[0] += 30
            return False

        async def revoke_family(self, fam, *, exp=None):
            self.family_exp = exp
            await super().revoke_family(fam, exp=exp)

    store = DelayedReplayStore()
    with pytest.raises(TokenRotationError, match="already used"):
        await rotate_refresh_token(
            pair.refresh_claims,
            store=store,
            secret=SECRET,
            refresh_ttl=ttl,
            leeway=leeway,
        )
    assert store.family_exp >= clock[0] + ttl + leeway
    assert await store.is_family_revoked(pair.refresh_claims["fam"])
