"""Regression: revoked refresh/logout state must never be read from a lagging Redis replica.

Run with local Redis available (DB 0 = primary, DB 1 = deliberately stale replica):
    uv run --with redis pytest -q -n 0 python/tests/test_jwt_redis_primary_regression.py

The second URL is a simulation of replica lag, not a real replication topology.
No FLUSHDB is used; keys have a unique prefix and are removed on teardown.
"""

import os
import uuid

import pytest
from django.test import override_settings

from django_bolt.auth import JWTAuthViews, create_token_pair
from django_bolt.auth.revocation import DjangoCacheRevocation
from django_bolt.exceptions import HTTPException


class _Routes:
    def __init__(self):
        self.handlers = {}

    def post(self, path, **_kwargs):
        def decorate(handler):
            self.handlers[path] = handler
            return handler

        return decorate


@pytest.mark.asyncio
async def test_lagging_redis_replica_cannot_hide_revoked_jti_family_or_version():
    """Keep write-primary state authoritative despite stale read-side data."""
    pytest.importorskip("redis")
    primary_url = os.environ.get("DJANGO_BOLT_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    lagging_url = os.environ.get("DJANGO_BOLT_TEST_REDIS_STALE_URL", "redis://127.0.0.1:6379/1")
    if primary_url == lagging_url:
        pytest.skip("The two test URLs must use isolated Redis databases")

    prefix = f"jwt-primary-regression:{uuid.uuid4().hex}:"
    user_id = "stale-replica-user"
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": [primary_url, lagging_url],
                "KEY_PREFIX": "jwt-primary-read-test",
            }
        }
    ):
        store = DjangoCacheRevocation(key_prefix=prefix)
        cache = store.cache
        primary = cache._cache.get_client(write=True)
        lagging = cache._cache.get_client(write=False)
        try:
            primary.ping()
            lagging.ping()
        except Exception as exc:
            pytest.skip(f"Both local Redis test databases must be available: {exc}")

        def key(suffix):
            return cache.make_and_validate_key(f"{prefix}{suffix}")

        old = create_token_pair(user_id, secret="jwt-primary-read-regression", version=0)
        old_jti = old.refresh_claims["jti"]
        old_family = old.refresh_claims["fam"]
        jti_key = key(old_jti)
        fam_key = key(f"fam:{old_family}")
        version_key = key(f"ver:{user_id}")
        try:
            await store.consume(old_jti, exp=old.refresh_claims["exp"])
            assert primary.exists(jti_key) == 1
            # The configured read target is deliberately stale.
            assert lagging.get(jti_key) is None
            assert cache.get(f"{prefix}{old_jti}") is None

            routes = _Routes()
            JWTAuthViews(store=store, secret="jwt-primary-read-regression").register(routes)
            logout_all = routes.handlers["/auth/logout-all"]
            with pytest.raises(HTTPException) as caught:
                await logout_all({"context": {"auth_claims": old.refresh_claims, "user_id": user_id}})
            assert caught.value.status_code == 401
            assert primary.get(version_key) is None
            assert await store.is_revoked(old_jti) is True

            await store.revoke_family(old_family, exp=old.refresh_claims["exp"])
            assert lagging.get(fam_key) is None
            assert await store.is_family_revoked(old_family) is True

            assert await store.bump_user_version_if_current(user_id, 0) is True
            assert lagging.get(version_key) is None
            assert await store.get_user_version(user_id) == 1
        finally:
            primary.delete(jti_key, fam_key, version_key)
            cache.close()
