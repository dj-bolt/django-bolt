"""Security and concurrency regression tests for the Redis Lua JWT logout path.

Must use a throwaway local Redis. Never FLUSHDB; all keys are scoped and removed.
Run with DJANGO_BOLT_TEST_REDIS_URL=redis://127.0.0.1:6380/0.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlparse

import pytest
from django.core.cache.backends.redis import RedisCache
from django.test import override_settings

from django_bolt.auth import DjangoCacheRevocation, JWTAuthViews, create_token_pair
from django_bolt.exceptions import HTTPException

pytestmark = pytest.mark.django_db
SECRET = "bolt-jwt-lua-tests-test-secret-at-least-32-chars"


class Routes:
    def __init__(self):
        self.handlers = {}

    def post(self, path, **_kwargs):
        def decorate(handler):
            self.handlers[path] = handler
            return handler

        return decorate


def _handler(store, endpoint="/auth/logout"):
    api = Routes()
    JWTAuthViews(store=store, secret=SECRET).register(api)
    return api.handlers[endpoint]


def _request(claims):
    return {"context": {"auth_claims": claims, "user_id": claims["sub"]}}


def _run(handler, claims):
    return asyncio.run(handler(_request(claims)))


@pytest.fixture
def live_redis():
    pytest.importorskip("redis")
    url = os.environ.get("DJANGO_BOLT_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set DJANGO_BOLT_TEST_REDIS_URL to an isolated local Redis to run Lua integration")
    if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
        pytest.fail("Safety guard: Redis integration tests require a LOCAL address")
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": url,
                "KEY_PREFIX": "bolt-jwt-lua-integration",
            }
        }
    ):
        prefix = f"lua-test:{uuid.uuid4().hex}:"
        store = DjangoCacheRevocation(key_prefix=prefix)
        client = store.cache._cache.get_client(write=True)
        client.ping()
        try:
            yield store, client
        finally:
            keys = list(client.scan_iter(match=f"*{prefix}*", count=1000))
            if keys:
                client.delete(*keys)
            store.cache.close()


def _keys(store, claims):
    prefix = store.key_prefix
    return (
        store.cache.make_and_validate_key(f"{prefix}{claims['jti']}"),
        store.cache.make_and_validate_key(f"{prefix}fam:{claims['fam']}"),
        store.cache.make_and_validate_key(f"{prefix}ver:{claims['sub']}"),
    )


def _assert_invalid(handler, claims):
    with pytest.raises(HTTPException) as caught:
        _run(handler, claims)
    assert caught.value.status_code == 401


@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all"])
@pytest.mark.parametrize("prebumped", [False, True])
def test_legacy_missing_version_preserves_zero_semantics(live_redis, endpoint, prebumped):
    store, client = live_redis
    claims = create_token_pair("missing-version-user", secret=SECRET).refresh_claims
    claims.pop("ver")
    jti_key, family_key, version_key = _keys(store, claims)
    handler = _handler(store, endpoint)

    if prebumped:
        assert asyncio.run(store.bump_user_version_if_current(claims["sub"], 0))
        _assert_invalid(handler, claims)
        assert client.get(jti_key) is None
        assert client.get(family_key) is None
        assert asyncio.run(store.get_user_version(claims["sub"])) == 1
        return

    assert _run(handler, claims).status_code == 200
    assert client.get(jti_key) is None
    if endpoint == "/auth/logout":
        assert client.get(family_key) is not None
        assert client.get(version_key) is None
    else:
        assert client.get(family_key) is None
        assert asyncio.run(store.get_user_version(claims["sub"])) == 1
    _assert_invalid(handler, claims)


def test_lua_success_uses_django_serializer_and_long_enough_ttl(live_redis):
    store, client = live_redis
    claims = create_token_pair("lua-success-user", secret=SECRET).refresh_claims
    handler = _handler(store)
    response = _run(handler, claims)
    assert response.status_code == 200
    jti_key, fam_key, _ = _keys(store, claims)
    raw = client.get(fam_key)
    assert raw is not None
    assert store.cache._cache._serializer.loads(raw) == "1"
    assert store.cache.get(f"{store.key_prefix}fam:{claims['fam']}") == "1"
    assert client.get(jti_key) is None
    assert client.ttl(fam_key) >= 604800 - 5
    _assert_invalid(handler, claims)


@pytest.mark.parametrize("revoked_state", ["jti", "family", "version"])
def test_lua_rejects_each_invalid_state_before_writing(live_redis, revoked_state):
    store, client = live_redis
    claims = create_token_pair("lua-blocked-user", secret=SECRET).refresh_claims
    jti_key, fam_key, ver_key = _keys(store, claims)
    if revoked_state == "jti":
        asyncio.run(store.consume(claims["jti"], exp=claims["exp"]))
    elif revoked_state == "family":
        asyncio.run(store.revoke_family(claims["fam"], exp=claims["exp"]))
    else:
        assert asyncio.run(store.bump_user_version_if_current(claims["sub"], 0))
    _assert_invalid(_handler(store), claims)
    if revoked_state != "family":
        assert client.get(fam_key) is None
    if revoked_state != "version":
        assert client.get(ver_key) is None
    if revoked_state != "jti":
        assert client.get(jti_key) is None


def test_lua_preserves_short_expiry_and_version_with_large_integer(live_redis):
    store, client = live_redis
    uid = "large-claim-version-user"
    claims = create_token_pair(uid, secret=SECRET).refresh_claims
    _, fam_key, ver_key = _keys(store, claims)
    # Treat Redis state as exact serialized bytes; no Lua floating-point rounding.
    value = 2**53 + 7
    client.set(ver_key, str(value))
    outcome = asyncio.run(
        store.revoke_family_if_active(
            claims["jti"],
            claims["fam"],
            uid,
            value,
            exp=int(time.time()) + 20,
        )
    )
    assert outcome is True
    assert 1 <= client.ttl(fam_key) <= 20


class DenyFamilyStore(DjangoCacheRevocation):
    async def is_family_revoked(self, fam):
        return True


class RecordWriterStore(DjangoCacheRevocation):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.wrote_using_custom_writer = False

    async def revoke_family(self, fam, *, exp=None):
        self.wrote_using_custom_writer = True
        await super().revoke_family(fam, exp=exp)


@pytest.mark.parametrize("cls", [DenyFamilyStore, RecordWriterStore])
def test_custom_store_security_and_writer_are_not_bypassed(live_redis, cls):
    _store, client = live_redis
    store = cls(key_prefix=_store.key_prefix)
    claims = create_token_pair("custom-store-user", secret=SECRET).refresh_claims
    assert (
        asyncio.run(
            store.revoke_family_if_active(
                claims["jti"],
                claims["fam"],
                claims["sub"],
                0,
                exp=int(time.time()) + 60,
            )
        )
        is None
    )
    if cls is DenyFamilyStore:
        _assert_invalid(_handler(store), claims)
        assert client.get(_keys(store, claims)[1]) is None
    else:
        assert _run(_handler(store), claims).status_code == 200
        assert store.wrote_using_custom_writer is True


class PolicyCache(RedisCache):
    def get(self, key, default=None, version=None):
        if ":fam:" in key:
            return "blocked by custom policy"
        return super().get(key, default=default, version=version)


class PolicyWriteCache(RedisCache):
    def set(self, key, value, timeout=None, version=None):
        raise RuntimeError("custom cache writer policy")


@pytest.mark.parametrize("backend", [PolicyCache, PolicyWriteCache])
def test_custom_rediscache_get_or_set_policy_is_respected(live_redis, backend):
    store, _client = live_redis
    # override_settings clears cached Django cache connections on exit.
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": f"{__name__}.{backend.__name__}",
                "LOCATION": os.environ["DJANGO_BOLT_TEST_REDIS_URL"],
                "KEY_PREFIX": "bolt-jwt-lua-integration",
            }
        }
    ):
        custom = DjangoCacheRevocation(key_prefix=store.key_prefix)
        claims = create_token_pair("policy-user", secret=SECRET).refresh_claims
        assert (
            asyncio.run(
                custom.revoke_family_if_active(
                    claims["jti"],
                    claims["fam"],
                    claims["sub"],
                    0,
                    exp=int(time.time()) + 3600,
                )
            )
            is None
        )
        if backend is PolicyCache:
            _assert_invalid(_handler(custom), claims)
        else:
            with pytest.raises(RuntimeError, match="custom cache writer"):
                _run(_handler(custom), claims)


def test_backend_script_failure_does_not_fall_back_to_insecure_write(monkeypatch, live_redis):
    store, client = live_redis
    claims = create_token_pair("script-fails-user", secret=SECRET).refresh_claims
    calls = 0

    def fail_evalsha(_client, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("injected Redis Lua failure")

    # Django's RedisCacheClient.get_client() makes a NEW Redis instance per call.
    # Patch the method on the class to intercept every instance in this test.
    monkeypatch.setattr(type(client), "evalsha", fail_evalsha)
    with pytest.raises(RuntimeError, match="injected Redis Lua failure"):
        _run(_handler(store), claims)
    assert calls == 1, "The Lua fast path must reach Redis EVALSHA exactly once"
    # Failure must never silently fall back to an unverified family write.
    assert store.cache.get(f"{store.key_prefix}fam:{claims['fam']}") is None


def test_many_simultaneous_logouts_have_exactly_one_winner(live_redis):
    store, client = live_redis
    claims = create_token_pair("same-session-user", secret=SECRET).refresh_claims
    handler = _handler(store)
    threads = 16
    gate = Barrier(threads)

    def attempt():
        gate.wait(timeout=10)
        try:
            _run(handler, claims)
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=threads) as pool:
        statuses = list(pool.map(lambda _: attempt(), range(threads)))
    assert statuses.count(200) == 1, statuses
    assert statuses.count(401) == threads - 1, statuses
    assert client.get(_keys(store, claims)[1]) is not None


def test_logout_vs_logout_all_security_under_race(live_redis):
    store, client = live_redis
    logout = _handler(store, "/auth/logout")
    logout_all = _handler(store, "/auth/logout-all")
    for idx in range(20):
        claims = create_token_pair(f"racing-user-{idx}", secret=SECRET).refresh_claims
        gate = Barrier(2)

        def attempt(handler, *, gate=gate, claims=claims):
            gate.wait(timeout=10)
            try:
                _run(handler, claims)
                return 200
            except HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, handler) for handler in (logout, logout_all)]
            statuses = [future.result() for future in futures]
        # Both can report success if logout-all validated just before logout's
        # Lua write. Their security effects must nonetheless remain permanent.
        assert all(status in (200, 401) for status in statuses), statuses
        assert 200 in statuses, statuses
        _jti_key, fam_key, ver_key = _keys(store, claims)
        assert client.get(fam_key) is not None or client.get(ver_key) is not None
        for handler in (logout, logout_all):
            with pytest.raises(HTTPException) as denied:
                _run(handler, claims)
            assert denied.value.status_code == 401
