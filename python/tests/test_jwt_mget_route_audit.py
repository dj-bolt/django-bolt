"""Focused Redis integration tests: both JWT logout endpoints use one primary MGET.

Run within django-bolt's python/tests directory; requires a LOCAL TEST Redis.
Does not modify source or flush Redis; uses random prefixes and removes own keys.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from django.core.cache.backends.redis import RedisCache
from django.test import override_settings

from django_bolt.auth import (
    DjangoCacheRevocation,
    JWTAuthViews,
    TokenRotationError,
    create_token_pair,
    rotate_refresh_token,
)
from django_bolt.exceptions import HTTPException

pytestmark = pytest.mark.django_db

SECRET = "jwt-mget-endpoint-integration-test-key"


class _Routes:
    def __init__(self):
        self.handlers = {}

    def post(self, path, **_kwargs):
        def register(fn):
            self.handlers[path] = fn
            return fn

        return register


def _settings():
    return {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.environ.get("DJANGO_BOLT_TEST_REDIS_URL", "redis://127.0.0.1:6379/15"),
            "KEY_PREFIX": "bolt-jwt-mget-endpoint-test",
        }
    }


def _setup():
    """Prepare Redis-backed auth routes and token keys for auditing."""
    pytest.importorskip("redis")
    store = DjangoCacheRevocation(key_prefix=f"mget-routes:{uuid.uuid4().hex}:")
    backend = store.cache._cache
    client = backend.get_client(write=True)
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"Test Redis unavailable: {exc}")
    routes = _Routes()
    JWTAuthViews(store=store, secret=SECRET).register(routes)
    pair = create_token_pair("mget-route-user", secret=SECRET)
    request = {"context": {"auth_claims": pair.refresh_claims, "user_id": "mget-route-user"}}
    prefix = store.key_prefix
    fam_key = store.cache.make_and_validate_key(f"{prefix}fam:{pair.refresh_claims['fam']}")
    ver_key = store.cache.make_and_validate_key(f"{prefix}ver:mget-route-user")
    jti_key = store.cache.make_and_validate_key(f"{prefix}{pair.refresh_claims['jti']}")
    return backend, client, routes, request, (fam_key, ver_key, jti_key)


@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all"])
def test_each_logout_uses_one_primary_mget_and_no_individual_get(monkeypatch, endpoint):
    """Count primary batch reads and avoid separate key reads in logout."""
    with override_settings(CACHES=_settings()):
        backend, original_client, routes, request, keys = _setup()
        get_client = backend.get_client
        calls = {
            "mget": 0,
            "get": 0,
            "evalsha": 0,
            "eval": 0,
            "primary_mget": 0,
            "primary_evalsha": 0,
            "primary_eval": 0,
        }
        tracked = set()

        def tracked_factory(*args, **kwargs):
            """Wrap each Redis client once to count backend operations."""
            client = get_client(*args, **kwargs)
            if id(client) in tracked:
                return client
            tracked.add(id(client))
            write = kwargs.get("write") is True

            def wrap(method, name):
                """Instrument a Redis client method for operation counting."""
                def counted(*items, **opts):
                    """Count calls and mark operations sent to the write primary."""
                    calls[name] += 1
                    if write and name in ("mget", "evalsha", "eval"):
                        calls[f"primary_{name}"] += 1
                    return method(*items, **opts)

                return counted

            for name in ("mget", "get", "evalsha", "eval"):
                monkeypatch.setattr(client, name, wrap(getattr(client, name), name))
            return client

        try:
            monkeypatch.setattr(backend, "get_client", tracked_factory)
            response = asyncio.run(routes.handlers[endpoint](request))
            assert response.status_code == 200
            assert calls["get"] == 0
            if endpoint == "/auth/logout":
                # The MGET is now run inside server-side Lua, not via redis-py mget().
                assert calls["mget"] == 0
                assert calls["evalsha"] >= 1  # First call may retry NOSCRIPT.
                assert calls["primary_evalsha"] >= 1
                assert original_client.get(keys[0]) is not None
                assert original_client.get(keys[1]) is None
            else:
                assert calls["mget"] == 1
                assert calls["primary_mget"] == 1
                assert calls["eval"] == 1
                assert calls["primary_eval"] == 1
                assert original_client.get(keys[0]) is None
                version_raw = original_client.get(keys[1])
                assert backend._serializer.loads(version_raw) == 1
        finally:
            original_client.delete(*keys)


@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all"])
def test_mget_failure_never_performs_logout_mutation(monkeypatch, endpoint):
    """Reject logout without mutation when security-state reads fail."""
    with override_settings(CACHES=_settings()):
        backend, original_client, routes, request, keys = _setup()
        get_client = backend.get_client

        def failing_factory(*args, **kwargs):
            """Inject a failure into the relevant Redis security operation."""
            client = get_client(*args, **kwargs)
            if kwargs.get("write") is True:

                def unavailable(*_args, **_kwargs):
                    raise RuntimeError("simulated security read outage")

                # Logout uses Lua evalsha; logout-all still uses direct MGET.
                if endpoint == "/auth/logout":
                    monkeypatch.setattr(client, "evalsha", unavailable)
                else:
                    monkeypatch.setattr(client, "mget", unavailable)
            return client

        try:
            monkeypatch.setattr(backend, "get_client", failing_factory)
            with pytest.raises(RuntimeError, match="simulated security read outage"):
                asyncio.run(routes.handlers[endpoint](request))
            assert original_client.get(keys[0]) is None
            assert original_client.get(keys[1]) is None
        finally:
            original_client.delete(*keys)


# Backward compatibility: optional MGET must not silently bypass custom
# security checks provided by DjangoCacheRevocation subclasses.
class _ExtraJTIRevocation(DjangoCacheRevocation):
    async def is_revoked(self, jti: str) -> bool:
        return True


class _ExtraFamilyRevocation(DjangoCacheRevocation):
    async def is_family_revoked(self, fam: str) -> bool:
        return True


class _ExtraVersionPolicy(DjangoCacheRevocation):
    async def get_user_version(self, user_id: str) -> int:
        return 37


@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all"], ids=["logout", "logout-all"])
@pytest.mark.parametrize(
    "store_class",
    [_ExtraJTIRevocation, _ExtraFamilyRevocation, _ExtraVersionPolicy],
    ids=["custom-jti", "custom-family", "custom-version"],
)
def test_subclass_security_overrides_not_skipped_by_mget(store_class, endpoint):
    """Preserve custom store policies when the MGET path is available."""
    with override_settings(CACHES=_settings()):
        store = store_class(key_prefix=f"mget-custom:{uuid.uuid4().hex}:")
        backend = store.cache._cache
        client = backend.get_client(write=True)
        try:
            client.ping()
        except Exception as exc:
            pytest.skip(f"Test Redis unavailable: {exc}")

        routes = _Routes()
        JWTAuthViews(store=store, secret=SECRET).register(routes)
        user_id = "mget-custom-override-user"
        pair = create_token_pair(user_id, secret=SECRET)
        request = {"context": {"auth_claims": pair.refresh_claims, "user_id": user_id}}
        prefix = store.key_prefix
        fam_key = store.cache.make_and_validate_key(f"{prefix}fam:{pair.refresh_claims['fam']}")
        ver_key = store.cache.make_and_validate_key(f"{prefix}ver:{user_id}")
        jti_key = store.cache.make_and_validate_key(f"{prefix}{pair.refresh_claims['jti']}")
        try:
            with pytest.raises(HTTPException) as error:
                asyncio.run(routes.handlers[endpoint](request))
            assert error.value.status_code == 401
            assert client.get(fam_key) is None, "Invalid logout must not revoke a family"
            assert client.get(ver_key) is None, "Invalid logout-all must not bump the user version"
        finally:
            client.delete(fam_key, ver_key, jti_key)


# Regression: the shared refresh validator must reject family-less tokens
# before any Redis/cache state read or logout mutation.
@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all"])
@pytest.mark.parametrize("family", [None, "", 123, []], ids=["missing", "empty", "integer", "list"])
def test_logout_rejects_invalid_family_before_store_access(monkeypatch, endpoint, family):
    """Reject invalid family claims before any revocation-store call."""
    with override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}):
        store = DjangoCacheRevocation(key_prefix=f"mget-family:{uuid.uuid4().hex}:")
        routes = _Routes()
        JWTAuthViews(store=store, secret=SECRET).register(routes)
        pair = create_token_pair("familyless-user", secret=SECRET)
        claims = dict(pair.refresh_claims)
        if family is None:
            claims.pop("fam")
        else:
            claims["fam"] = family

        async def must_not_access_store(*_args, **_kwargs):
            raise AssertionError("Invalid family must be rejected before state access")

        monkeypatch.setattr(store, "get_refresh_state", must_not_access_store)
        monkeypatch.setattr(store, "revoke_family", must_not_access_store)
        monkeypatch.setattr(store, "bump_user_version_if_current", must_not_access_store)
        request = {"context": {"auth_claims": claims, "user_id": "familyless-user"}}
        with pytest.raises(HTTPException) as error:
            asyncio.run(routes.handlers[endpoint](request))
        assert error.value.status_code == 401
        assert asyncio.run(store.get_user_version("familyless-user")) == 0


# Regression: a custom Django RedisCache.get can perform extra security checks.
# The built-in RedisCache must still use MGET on the write primary.
class _CustomPolicyRedisCache(RedisCache):
    def get(self, key, default=None, version=None):
        """Return the custom family-revocation policy for cache reads."""
        if ":fam:" in key:
            return "revoked-by-custom-cache-policy"
        return super().get(key, default=default, version=version)


@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all"])
def test_custom_rediscache_get_policy_is_not_bypassed(endpoint):
    """Honor a RedisCache get override on both logout routes."""
    pytest.importorskip("redis")
    settings = _settings()
    settings["default"]["BACKEND"] = f"{__name__}._CustomPolicyRedisCache"

    with override_settings(CACHES=settings):
        store = DjangoCacheRevocation(key_prefix=f"mget-custom-get:{uuid.uuid4().hex}:")
        cache = store.cache
        backend = cache._cache
        client = backend.get_client(write=True)
        try:
            client.ping()
        except Exception as exc:
            pytest.skip(f"Test Redis unavailable: {exc}")

        user_id = "custom-get-policy-user"
        pair = create_token_pair(user_id, secret=SECRET)
        claims = pair.refresh_claims
        prefix = store.key_prefix
        fam_key = cache.make_and_validate_key(f"{prefix}fam:{claims['fam']}")
        ver_key = cache.make_and_validate_key(f"{prefix}ver:{user_id}")
        jti_key = cache.make_and_validate_key(f"{prefix}{claims['jti']}")
        try:
            # Returning None requests method-based fallback, which must call
            # the custom cache.get rather than directly querying Redis.
            assert asyncio.run(store.get_refresh_state(claims["jti"], claims["fam"], user_id)) is None
            assert asyncio.run(store.is_family_revoked(claims["fam"])) is True
            routes = _Routes()
            JWTAuthViews(store=store, secret=SECRET).register(routes)
            request = {"context": {"auth_claims": claims, "user_id": user_id}}
            with pytest.raises(HTTPException) as error:
                asyncio.run(routes.handlers[endpoint](request))
            assert error.value.status_code == 401
            assert client.get(fam_key) is None  # No real mutation occurred.
            assert client.get(ver_key) is None
        finally:
            client.delete(fam_key, ver_key, jti_key)
            cache.close()


class _InheritedRedisCache(RedisCache):
    pass


def test_inherited_rediscache_reads_primary_and_batches(monkeypatch):
    """Keep primary batching for RedisCache subclasses without overrides."""
    pytest.importorskip("redis")
    settings = _settings()
    settings["default"]["BACKEND"] = f"{__name__}._InheritedRedisCache"
    with override_settings(CACHES=settings):
        store = DjangoCacheRevocation(key_prefix=f"mget-subclass:{uuid.uuid4().hex}:")
        cache = store.cache
        backend = cache._cache
        client = backend.get_client(write=True)
        try:
            client.ping()
        except Exception as exc:
            pytest.skip(f"Test Redis unavailable: {exc}")
        pair = create_token_pair("subclass-user", secret=SECRET)
        calls = []
        original_get_client = backend.get_client

        def tracking_client(*args, **kwargs):
            calls.append(kwargs.get("write"))
            return original_get_client(*args, **kwargs)

        monkeypatch.setattr(backend, "get_client", tracking_client)
        prefix = store.key_prefix
        keys = [
            cache.make_and_validate_key(f"{prefix}{pair.refresh_claims['jti']}"),
            cache.make_and_validate_key(f"{prefix}fam:{pair.refresh_claims['fam']}"),
            cache.make_and_validate_key(f"{prefix}ver:subclass-user"),
        ]
        try:
            state = asyncio.run(
                store.get_refresh_state(pair.refresh_claims["jti"], pair.refresh_claims["fam"], "subclass-user")
            )
            assert state == (False, False, 0)
            assert calls == [True]
        finally:
            client.delete(*keys)
            cache.close()


class _CustomUnderlyingStateReader(DjangoCacheRevocation):
    def _get_revocation_state(self, key):
        """Simulate a custom reader that marks the family as revoked."""
        if ":fam:" in key:
            return "revoked-by-custom-reader"
        return super()._get_revocation_state(key)


@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all", "/auth/refresh"])
def test_custom_underlying_revocation_reader_is_not_bypassed(endpoint):
    """Custom lower-level revocation policy must bypass both MGET and Lua."""
    pytest.importorskip("redis")
    with override_settings(CACHES=_settings()):
        store = _CustomUnderlyingStateReader(key_prefix=f"custom-underlying:{uuid.uuid4().hex}:")
        cache = store.cache
        client = cache._cache.get_client(write=True)
        try:
            client.ping()
        except Exception as exc:
            pytest.skip(f"Test Redis unavailable: {exc}")

        user_id = "custom-underlying-policy-user"
        claims = create_token_pair(user_id, secret=SECRET).refresh_claims
        fam_key = cache.make_and_validate_key(f"{store.key_prefix}fam:{claims['fam']}")
        ver_key = cache.make_and_validate_key(f"{store.key_prefix}ver:{user_id}")
        jti_key = cache.make_and_validate_key(f"{store.key_prefix}{claims['jti']}")
        try:
            assert asyncio.run(store.get_refresh_state(claims["jti"], claims["fam"], user_id)) is None
            assert asyncio.run(store.is_family_revoked(claims["fam"])) is True
            if endpoint == "/auth/refresh":
                with pytest.raises(TokenRotationError, match="family revoked"):
                    asyncio.run(rotate_refresh_token(claims, store=store, secret=SECRET))
            else:
                routes = _Routes()
                JWTAuthViews(store=store, secret=SECRET).register(routes)
                with pytest.raises(HTTPException) as error:
                    asyncio.run(routes.handlers[endpoint]({"context": {"auth_claims": claims, "user_id": user_id}}))
                assert error.value.status_code == 401
            assert client.get(fam_key) is None
            assert client.get(ver_key) is None
            assert client.get(jti_key) is None
        finally:
            client.delete(fam_key, ver_key, jti_key)
            cache.close()
