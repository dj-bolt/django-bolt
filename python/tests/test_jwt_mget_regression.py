"""Focused tests for batching JWT logout security reads without changing policy."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from django.test import override_settings

from django_bolt.auth import DjangoCacheRevocation, JWTAuthViews, create_token_pair
from django_bolt.exceptions import HTTPException

pytestmark = pytest.mark.django_db
SECRET = "jwt-mget-regression-test-secret"


class _Routes:
    def __init__(self):
        self.handlers = {}

    def post(self, path, **_kwargs):
        def register(fn):
            self.handlers[path] = fn
            return fn

        return register


def _handler(store, endpoint="/auth/logout-all"):
    routes = _Routes()
    JWTAuthViews(store=store, secret=SECRET).register(routes)
    return routes.handlers[endpoint]


def _request(pair, user_id):
    return {"context": {"auth_claims": pair.refresh_claims, "user_id": user_id}}


def _redis_settings(url):
    return {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": url,
            "KEY_PREFIX": "jwt-batch-regression-tests",
        }
    }


def _require_redis(store):
    """Skip Redis checks if the write-side client is unavailable."""
    pytest.importorskip("redis")
    try:
        client = store.cache._cache.get_client(write=True)
        client.ping()
    except Exception as exc:
        pytest.skip(f"Redis unavailable: {exc}")
    return client


def test_redis_mget_reads_revocation_version_and_family_from_primary(monkeypatch):
    """Read JTI, family, and user version together from the Redis primary."""
    url = os.environ.get("DJANGO_BOLT_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    with override_settings(CACHES=_redis_settings(url)):
        prefix = f"mget:{uuid.uuid4().hex}:"
        store = DjangoCacheRevocation(key_prefix=prefix)
        client = _require_redis(store)
        jti, fam, user_id = "token-1", "family-1", "user-1"
        keys = [f"{prefix}{jti}", f"{prefix}fam:{fam}", f"{prefix}ver:{user_id}"]
        raw_keys = [store.cache.make_and_validate_key(key) for key in keys]
        backend = store.cache._cache
        original_get_client = backend.get_client
        seen = []

        def tracked_client(*args, **kwargs):
            seen.append(kwargs.get("write"))
            return original_get_client(*args, **kwargs)

        try:
            monkeypatch.setattr(backend, "get_client", tracked_client)
            assert asyncio.run(store.get_refresh_state(jti, fam, user_id)) == (False, False, 0)
            assert seen == [True]  # One primary MGET, not three Redis GETs.
            assert asyncio.run(store.get_refresh_state(jti, None, user_id)) == (False, False, 0)
            awaitable = store.revoke(jti, exp=None)
            asyncio.run(awaitable)
            assert asyncio.run(store.get_refresh_state(jti, fam, user_id)) == (True, False, 0)
            asyncio.run(store.revoke_family(fam))
            assert asyncio.run(store.get_refresh_state(jti, fam, user_id)) == (True, True, 0)
            asyncio.run(store.bump_user_version(user_id))
            assert asyncio.run(store.get_refresh_state(jti, fam, user_id)) == (True, True, 1)
        finally:
            client.delete(*raw_keys)


def test_redis_mget_preserves_logout_all_and_replay_rejections():
    url = os.environ.get("DJANGO_BOLT_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    with override_settings(CACHES=_redis_settings(url)):
        prefix = f"mget:{uuid.uuid4().hex}:"
        store = DjangoCacheRevocation(key_prefix=prefix)
        client = _require_redis(store)
        user_id = "user-2"
        pair = create_token_pair(user_id, secret=SECRET)
        second = create_token_pair(user_id, secret=SECRET)
        raw_keys = [store.cache.make_and_validate_key(f"{prefix}ver:{user_id}")]
        try:
            handler = _handler(store)
            response = asyncio.run(handler(_request(pair, user_id)))
            assert response.status_code == 200
            assert asyncio.run(store.get_user_version(user_id)) == 1
            with pytest.raises(HTTPException) as exc:
                asyncio.run(handler(_request(second, user_id)))
            assert exc.value.status_code == 401
            assert asyncio.run(store.get_user_version(user_id)) == 1
        finally:
            client.delete(*raw_keys)


def test_locmem_uses_fallback_checks_and_version_cas():
    with override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}):
        store = DjangoCacheRevocation(key_prefix=f"mget:{uuid.uuid4().hex}:")
        assert asyncio.run(store.get_refresh_state("j", "f", "u")) is None
        pair = create_token_pair("u", secret=SECRET)
        assert asyncio.run(_handler(store)(_request(pair, "u"))).status_code == 200
        assert asyncio.run(store.get_user_version("u")) == 1
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_handler(store)(_request(pair, "u")))
        assert exc.value.status_code == 401


def test_mget_read_errors_never_accept_refresh(monkeypatch):
    """Fail closed when the primary batch read fails."""
    url = os.environ.get("DJANGO_BOLT_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    with override_settings(CACHES=_redis_settings(url)):
        store = DjangoCacheRevocation(key_prefix=f"mget:{uuid.uuid4().hex}:")
        _require_redis(store)
        backend = store.cache._cache
        # Django constructs a fresh Redis client on each get_client() call.
        # Mock the factory, not a single client instance that production never uses.
        original_get_client = backend.get_client

        def broken_mget(_keys):
            raise RuntimeError("simulated Redis read failure")

        def failing_primary_client(*args, **kwargs):
            """Inject a failed batch read into primary Redis clients."""
            client = original_get_client(*args, **kwargs)
            if kwargs.get("write") is True:
                monkeypatch.setattr(client, "mget", broken_mget)
            return client

        monkeypatch.setattr(backend, "get_client", failing_primary_client)
        pair = create_token_pair("user-fail-closed", secret=SECRET)
        with pytest.raises(RuntimeError, match="simulated Redis read failure"):
            asyncio.run(_handler(store)(_request(pair, "user-fail-closed")))
