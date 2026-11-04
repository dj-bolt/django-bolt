"""Real-Redis concurrency check for JWT global logout compare-and-bump.

Requires: uv run --with redis pytest -q -n 0 python/tests/test_jwt_atomic_redis_integration.py
Defaults to local Redis DB 0 and uses unique keys; never flushes Redis.
Override the endpoint using DJANGO_BOLT_TEST_REDIS_URL.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.test import override_settings

from django_bolt.auth import JWTAuthViews, create_token_pair
from django_bolt.auth.revocation import DjangoCacheRevocation
from django_bolt.exceptions import HTTPException

pytestmark = pytest.mark.django_db


def test_redis_atomic_compare_and_bump_under_concurrent_clients():
    """Allow only one conditional version bump during a Redis race."""
    pytest.importorskip("redis")
    url = os.environ.get("DJANGO_BOLT_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    cache_conf = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": url,
            "KEY_PREFIX": "bolt-jwt-atomic-tests",
        }
    }
    prefix = f"atomic:{uuid.uuid4()}:"
    user_id = "concurrent"
    with override_settings(CACHES=cache_conf):
        probe = DjangoCacheRevocation(key_prefix=prefix)
        try:
            client = probe.cache._cache.get_client(write=True)
            client.ping()
        except Exception as exc:
            pytest.fail(f"Redis is required for this integration test: {exc}")

        # Each worker has a different store object/cache client. A barrier
        # releases all calls together, not just async tasks on one event loop.
        count = 12
        ready = Barrier(count)

        def attempt(_index):
            store = DjangoCacheRevocation(key_prefix=prefix)
            ready.wait(timeout=15)
            return asyncio.run(store.bump_user_version_if_current(user_id, 0))

        try:
            with ThreadPoolExecutor(max_workers=count) as pool:
                results = list(pool.map(attempt, range(count)))

            assert results.count(True) == 1
            assert results.count(False) == count - 1
            assert asyncio.run(DjangoCacheRevocation(key_prefix=prefix).get_user_version(user_id)) == 1

            # The marker is permanent, not a short-TTL revocation entry.
            raw_key = probe.cache.make_key(f"{prefix}ver:{user_id}")
            assert client.ttl(raw_key) == -1
            assert asyncio.run(probe.bump_user_version_if_current(user_id, 0)) is False
            assert asyncio.run(probe.bump_user_version_if_current(user_id, 1)) is True
            assert asyncio.run(probe.get_user_version(user_id)) == 2
        finally:
            # Clean only this test's uniquely prefixed version key.
            client.delete(probe.cache.make_key(f"{prefix}ver:{user_id}"))


def test_locmem_atomic_compare_and_bump():
    with override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}):
        store = DjangoCacheRevocation(key_prefix=f"atomic:{uuid.uuid4()}:")
        assert asyncio.run(store.bump_user_version_if_current("user", 0)) is True
        assert asyncio.run(store.bump_user_version_if_current("user", 0)) is False
        assert asyncio.run(store.get_user_version("user")) == 1
        assert asyncio.run(store.bump_user_version_if_current("user", 1)) is True
        assert asyncio.run(store.get_user_version("user")) == 2


@pytest.mark.asyncio
async def test_logout_all_unsupported_cache_returns_503_without_version_change():

    class Routes:
        def __init__(self):
            self.handlers = {}

        def post(self, path, **_kwargs):
            def register(handler):
                self.handlers[path] = handler
                return handler

            return register

    with override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}):
        store = DjangoCacheRevocation(key_prefix=f"atomic:{uuid.uuid4()}:")
        routes = Routes()
        JWTAuthViews(store=store, secret="jwt-atomic-compat-test").register(routes)

        pair = create_token_pair("user", secret="jwt-atomic-compat-test", version=0)
        request = {"context": {"auth_claims": pair.refresh_claims, "user_id": "user"}}

        with pytest.raises(HTTPException) as exc:
            await routes.handlers["/auth/logout-all"](request)

        assert exc.value.status_code == 503
        assert await store.get_user_version("user") == 0
