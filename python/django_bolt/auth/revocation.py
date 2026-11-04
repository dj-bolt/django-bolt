"""
Token revocation support for Django-Bolt.

Provides flexible revocation strategies that users can choose based on their needs.
Revocation is OPTIONAL - only checked if user provides a handler.
"""

import pickle
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta

from django.apps import apps
from django.core.cache import caches
from django.core.cache.backends.base import BaseCache
from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.memcached import PyMemcacheCache
from django.core.cache.backends.redis import RedisCache
from django.db import IntegrityError

_DEFAULT_TTL_SECONDS = 86400 * 7  # 7 days — fallback when caller doesn't pass `exp`

# Django's RedisCache serializes integers as plain decimal strings. The version
# key never expires and CAS + INCR must happen in one server-side operation.
_REDIS_COMPARE_AND_BUMP = """
local current = redis.call('GET', KEYS[1])
if (not current and ARGV[1] == '0') or current == ARGV[1] then
    redis.call('INCR', KEYS[1])
    return 1
end
return 0
"""


# Redis Primary-only atomic JWT Logout. Keep every check in one short script.
# Keys: revoked JTI, revoked family, user version. The family key is written
# only after all three conditions pass. ARGV[2] is Django's serialized "1".
_REDIS_REVOKE_ACTIVE_FAMILY = """
local state = redis.call('MGET', KEYS[1], KEYS[2], KEYS[3])
if state[1] ~= false or state[2] ~= false then
    return 0
end
if (state[3] or '0') ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""


def _ttl_for(exp: int | None, default: int = _DEFAULT_TTL_SECONDS) -> int:
    """
    Seconds a revocation entry should live.

    Uses the caller-supplied absolute retention deadline. For JWTs,
    include the verifier's expiry leeway (token ``exp`` + leeway) to avoid
    removing a revocation marker while the token is still accepted. A past
    deadline clamps to 0 — we never hand a
    negative duration to ``timedelta``, which would silently subtract from
    now. When ``exp`` is ``None``, ``default`` is used — typically the
    store's ``default_ttl`` instance attribute.
    """
    if exp is None:
        return default
    return max(0, int(exp) - int(time.time()))


class RevocationStore(ABC):
    """
    Base class for token revocation storage.

    Implementations can use in-memory, Django cache, database, Redis, etc.
    """

    @abstractmethod
    async def is_revoked(self, jti: str) -> bool:
        """
        Check if a token (by JTI) is revoked.

        Args:
            jti: JWT ID (unique token identifier)

        Returns:
            True if token is revoked, False otherwise
        """
        pass

    @abstractmethod
    async def revoke(self, jti: str, *, exp: int | None = None) -> None:
        """
        Revoke a token.

        The revocation marker must remain until no configured verifier
        can accept the JWT, including any expiry leeway. For example, with
        a ``JWTAuthentication`` instance named ``auth``:

            await store.revoke(claims["jti"], exp=claims["exp"] + auth.leeway)

        Args:
            jti: JWT ID to revoke.
            exp: Absolute retention deadline (epoch seconds), including any
                JWT verifier leeway. If omitted, uses the store's fallback
                TTL (7 days by default); ensure it covers the token lifetime.
        """
        pass

    async def revoke_all(self, user_id: str) -> None:
        """
        Revoke all tokens for a user (optional, not all stores support this).

        Args:
            user_id: User identifier
        """
        raise NotImplementedError("This revocation store does not support revoke_all")

    async def consume(self, jti: str, *, exp: int | None = None) -> bool:
        """Atomically mark a refresh token as used.

        Returns ``True`` only for the first caller. Implementations used for
        rotating refresh tokens must provide an atomic check-and-set; a
        separate ``is_revoked()``/``revoke()`` sequence is vulnerable to two
        concurrent rotations both succeeding.
        """
        raise NotImplementedError("This revocation store does not support atomic token consumption")

    # --- Bulk primitives (optional) -------------------------------------
    # Per-``jti`` revocation only kills a single session. Two production
    # flows need more: "log out everywhere" (invalidate every outstanding
    # token for a user) and refresh-token reuse detection (kill an entire
    # rotation family when a rotated-out token is replayed). These are
    # expressed with a per-user token *version* and a per-family revoke,
    # both O(1) and TTL-cleaned — no key scanning required.

    async def get_user_version(self, user_id: str) -> int:
        """Current token version for a user (0 if never bumped).

        A token carrying a different ``ver`` is invalid and must be rejected.
        Used to implement global logout without enumerating jtis.
        """
        raise NotImplementedError("This revocation store does not support token versioning")

    async def bump_user_version(self, user_id: str) -> int:
        """Invalidate every outstanding token for a user (global logout).

        Increments and returns the user's token version. Tokens minted
        before the bump carry a lower ``ver`` and are rejected.
        """
        raise NotImplementedError("This revocation store does not support token versioning")

    async def bump_user_version_if_current(self, user_id: str, expected_version: int) -> bool:
        """Atomically increment only if the user version matches a token.

        Required for replay-safe global logout: get-then-increment is racy.
        Unsupported stores must fail closed, never fall back to a plain bump.
        """
        raise NotImplementedError("This revocation store does not support atomic version comparison")

    async def revoke_family(self, fam: str, *, exp: int | None = None) -> None:
        """Revoke an entire refresh-token rotation family (reuse detection).

        When a rotated-out refresh token is replayed, revoking its ``fam``
        invalidates the whole chain — the standard OAuth reuse-detection
        response.
        """
        raise NotImplementedError("This revocation store does not support family revocation")

    async def is_family_revoked(self, fam: str) -> bool:
        """Whether a refresh-token family has been revoked."""
        raise NotImplementedError("This revocation store does not support family revocation")


class InMemoryRevocation(RevocationStore):
    """
    Simple in-memory revocation store.

    ⚠️  WARNING: Only works in single-process mode. Not suitable for production
    with multiple workers. Use DjangoCacheRevocation for multi-process setups.

    Good for:
    - Development
    - Testing
    - Single-process deployments

    Example:
        ```python
        from django_bolt.auth import JWTAuthentication
        from django_bolt.auth.revocation import InMemoryRevocation

        revocation = InMemoryRevocation()

        auth = JWTAuthentication(
            secret=settings.SECRET_KEY,
            revocation_store=revocation,
        )

        # In logout endpoint
        @api.post("/logout")
        async def logout(request):
            claims = request["context"]["auth_claims"]
            await revocation.revoke(claims["jti"], exp=claims["exp"] + auth.leeway)
            return {"message": "Logged out"}
        ```
    """

    def __init__(self):
        self._revoked: set[str] = set()
        self._revoked_families: set[str] = set()
        self._user_versions: dict[str, int] = {}

    async def is_revoked(self, jti: str) -> bool:
        return jti in self._revoked

    async def revoke(self, jti: str, *, exp: int | None = None) -> None:
        self._revoked.add(jti)

    async def consume(self, jti: str, *, exp: int | None = None) -> bool:
        # There is no await between membership testing and insertion, so this
        # is atomic with respect to tasks on the single event loop supported
        # by this development-only store.
        if jti in self._revoked:
            return False
        self._revoked.add(jti)
        return True

    async def get_user_version(self, user_id: str) -> int:
        return self._user_versions.get(str(user_id), 0)

    async def bump_user_version(self, user_id: str) -> int:
        new_version = self._user_versions.get(str(user_id), 0) + 1
        self._user_versions[str(user_id)] = new_version
        return new_version

    async def bump_user_version_if_current(self, user_id: str, expected_version: int) -> bool:
        """Advance the user version only when the expected version matches."""
        # No await in this critical section. InMemoryRevocation is documented
        # for a single process/event loop, not distributed production workers.
        key = str(user_id)
        if self._user_versions.get(key, 0) != expected_version:
            return False
        self._user_versions[key] = expected_version + 1
        return True

    async def revoke_family(self, fam: str, *, exp: int | None = None) -> None:
        self._revoked_families.add(fam)

    async def is_family_revoked(self, fam: str) -> bool:
        return fam in self._revoked_families

    def clear(self) -> None:
        """Clear all revoked tokens (useful for testing)."""
        self._revoked.clear()
        self._revoked_families.clear()
        self._user_versions.clear()


class DjangoCacheRevocation(RevocationStore):
    """
    Django cache-based revocation store.

    Basic revocation works with any Django cache backend. Refresh rotation
    and user-version bumps additionally require a backend whose ``add`` and
    ``incr`` operations are atomic (Redis, Memcached, or LocMem in the
    documented single-process development mode). File-based cache is rejected
    for those operations because its ``add`` is a racy check-then-set.

    Supported backends include:
    - Redis (django.core.cache.backends.redis.RedisCache)
    - Memcached (django.core.cache.backends.memcached.PyMemcacheCache)
    - Database (django.core.cache.backends.db.DatabaseCache)
    - File-based (django.core.cache.backends.filebased.FileBasedCache)
    - Local memory (django.core.cache.backends.locmem.LocMemCache)

    ✅ Production-ready with Redis or Memcached across processes/workers
    ✅ Fast: Uses Django's cache framework (Redis ~50k ops/sec)
    ✅ Automatic cleanup: TTL handled by cache backend

    Example:
        ```python
        # settings.py
        CACHES = {
            'default': {
                'BACKEND': 'django.core.cache.backends.redis.RedisCache',
                'LOCATION': 'redis://127.0.0.1:6379/1',
            }
        }

        # api.py
        from django_bolt.auth import JWTAuthentication
        from django_bolt.auth.revocation import DjangoCacheRevocation

        revocation = DjangoCacheRevocation(cache_alias='default')

        auth = JWTAuthentication(
            secret=settings.SECRET_KEY,
            revocation_store=revocation,
        )

        # In logout endpoint — pass `exp` so the cache entry expires
        # exactly when the token would have anyway.
        @api.post("/logout")
        async def logout(request):
            claims = request["context"]["auth_claims"]
            await revocation.revoke(claims["jti"], exp=claims["exp"] + auth.leeway)
            return {"message": "Logged out"}
        ```
    """

    def __init__(
        self,
        cache_alias: str = "default",
        key_prefix: str = "revoked:",
        *,
        default_ttl: int = _DEFAULT_TTL_SECONDS,
    ):
        """
        Initialize Django cache-based revocation.

        Args:
            cache_alias: Django cache alias to use (default: 'default').
            key_prefix: Prefix for cache keys (default: 'revoked:').
            default_ttl: Fallback retention (in seconds) used when a caller
                invokes ``revoke(jti)`` without passing ``exp``. Defaults to
                7 days. Per-call ``exp`` always wins over this.
        """
        self.cache_alias = cache_alias
        self.key_prefix = key_prefix
        self.default_ttl = default_ttl
        self._cache = None

    @property
    def cache(self):
        """Lazy-load cache to avoid import issues."""
        if self._cache is None:
            self._cache = caches[self.cache_alias]
        return self._cache

    def _get_revocation_state(self, key: str):
        """Read security-sensitive Redis state from the write primary.

        Django RedisCache.get() routes reads to replicas when configured,
        which may lag revocations and allow a revoked refresh token to be
        reused. Preserve the backend's configured serializer and cache key.
        """
        cache = self.cache
        # Inherited RedisCache.get is safe to bypass; a custom get() is not.
        if isinstance(cache, RedisCache) and type(cache).get is RedisCache.get:
            cache_key = cache.make_and_validate_key(key)
            backend = cache._cache
            raw = backend.get_client(cache_key, write=True).get(cache_key)
            return None if raw is None else backend._serializer.loads(raw)
        return cache.get(key)

    async def get_refresh_state(self, jti: str, fam: str | None, user_id: str) -> tuple[bool, bool, int] | None:
        """Read refresh revocation state atomically from Redis primary.

        ``None`` asks JWTAuthViews to keep its existing per-field fallback
        for Memcached, LocMem and other Django cache backends. This is an
        optional store capability, not a new RevocationStore requirement.
        """
        cache = self.cache
        # Respect custom get() overrides; inherited readers use primary MGET.
        if not (isinstance(cache, RedisCache) and type(cache).get is RedisCache.get):
            return None

        # A custom store may override individual revocation/version checks.
        # Do not bypass those policies with the optimized Redis read path.
        if any(
            getattr(getattr(self, name), "__func__", None) is not getattr(DjangoCacheRevocation, name)
            for name in ("_get_revocation_state", "is_revoked", "is_family_revoked", "get_user_version")
        ):
            return None

        keys = [f"{self.key_prefix}{jti}"]
        if fam is not None:
            keys.append(f"{self.key_prefix}fam:{fam}")
        keys.append(f"{self.key_prefix}ver:{user_id}")
        cache_keys = [cache.make_and_validate_key(key) for key in keys]
        backend = cache._cache
        client = backend.get_client(cache_keys[0], write=True)
        raw_values = client.mget(cache_keys)
        if len(raw_values) != len(cache_keys):
            raise RuntimeError("Incomplete Redis JWT refresh state")
        values = [None if raw is None else backend._serializer.loads(raw) for raw in raw_values]
        family_revoked = fam is not None and values[1] is not None
        return values[0] is not None, family_revoked, int(values[-1] or 0)

    async def is_revoked(self, jti: str) -> bool:
        key = f"{self.key_prefix}{jti}"
        return self._get_revocation_state(key) is not None

    async def revoke(self, jti: str, *, exp: int | None = None) -> None:
        """
        Revoke a token by storing its JTI in the cache.

        Pass an absolute retention deadline that includes the JWT
        verifier's leeway so an accepted token cannot outlive its marker.

            await store.revoke(claims["jti"], exp=claims["exp"] + auth.leeway)

        If ``exp`` is omitted, the entry lives for ``self.default_ttl``
        seconds (set on the constructor).
        """
        key = f"{self.key_prefix}{jti}"
        self.cache.set(key, "1", timeout=_ttl_for(exp, self.default_ttl))

    def _require_atomic_cache(self) -> None:
        cache_type = type(self.cache)
        backend_modules = {cls.__module__ for cls in cache_type.__mro__}
        known_non_atomic = {
            "django.core.cache.backends.filebased",
            "django.core.cache.backends.dummy",
        }
        inherits_non_atomic_incr = getattr(cache_type, "incr", None) is BaseCache.incr
        if backend_modules.intersection(known_non_atomic) or inherits_non_atomic_incr:
            raise NotImplementedError(
                "DjangoCacheRevocation refresh rotation and version bumps require "
                "a cache backend with atomic add/incr operations; file-based, "
                "database, dummy, and other non-atomic caches are not supported"
            )

    async def consume(self, jti: str, *, exp: int | None = None) -> bool:
        self._require_atomic_cache()
        key = f"{self.key_prefix}{jti}"
        return bool(self.cache.add(key, "1", timeout=_ttl_for(exp, self.default_ttl)))

    async def get_user_version(self, user_id: str) -> int:
        """Read the current user version from the revocation cache."""
        return int(self._get_revocation_state(f"{self.key_prefix}ver:{user_id}") or 0)

    async def bump_user_version(self, user_id: str) -> int:
        # User-version keys never expire — they are the source of truth for
        # "log out everywhere" and must outlive any token they invalidate.
        self._require_atomic_cache()
        key = f"{self.key_prefix}ver:{user_id}"
        try:
            return self.cache.incr(key)
        except ValueError:
            # Seed atomically. If another caller won the add race, increment
            # its value so no concurrent logout event is lost.
            if self.cache.add(key, 1, timeout=None):
                return 1
            return self.cache.incr(key)

    async def bump_user_version_if_current(self, user_id: str, expected_version: int) -> bool:
        """Atomically bump the user version on supported Django cache backends.

        Redis uses Lua; Memcached uses gets/cas; LocMem uses its process-local
        lock. The generic cache API cannot express CAS. Unsupported backends
        fail closed instead of a racy get()/incr() fallback.
        """
        cache = self.cache
        version_key = f"{self.key_prefix}ver:{user_id}"

        if isinstance(cache, LocMemCache):
            # Use the same lock as Django's cache get/set/incr operations.
            # The development-only LocMem backend is not shared by processes.
            key = cache.make_and_validate_key(version_key)
            with cache._lock:
                current = 0 if cache._has_expired(key) else pickle.loads(cache._cache[key])
                if current != expected_version:
                    return False
                cache._set(key, pickle.dumps(expected_version + 1, cache.pickle_protocol), timeout=None)
                return True

        if isinstance(cache, PyMemcacheCache):
            # Django's HashClient supports memcached gets/cas on the same key.
            # A missing key must be seeded atomically with add(), not set().
            key = cache.make_and_validate_key(version_key)
            current, cas_token = cache._cache.gets(key)
            if current is None:
                return expected_version == 0 and bool(cache.add(version_key, 1, timeout=None))
            if int(current) != expected_version:
                return False
            return cache._cache.cas(key, expected_version + 1, cas_token, expire=0, noreply=False) is True

        redis_backend = any(
            cls.__module__ == "django.core.cache.backends.redis" and cls.__name__ == "RedisCache"
            for cls in type(cache).__mro__
        )
        if not redis_backend:
            raise NotImplementedError("Atomic JWT logout-all requires RedisCache, PyMemcacheCache, or LocMemCache")

        # Cache key transformations must match get_user_version(); choose the
        # write primary rather than a potentially lagging Redis read replica.
        key = cache.make_key(version_key)
        client = cache._cache.get_client(key, write=True)
        return bool(client.eval(_REDIS_COMPARE_AND_BUMP, 1, key, str(expected_version)))

    async def revoke_family_if_active(
        self,
        jti: str,
        fam: str,
        user_id: str,
        expected_version: int,
        *,
        exp: int | None = None,
    ) -> bool | None:
        """Atomically verify and revoke on standard Redis; None requests fallback.

        Never bypass custom store readers/writers or custom RedisCache get/set.
        The read and write use the same Redis primary, with no race between them.
        """
        cache = self.cache
        if not isinstance(cache, RedisCache):
            return None
        if (
            getattr(cache.get, "__func__", None) is not RedisCache.get
            or getattr(cache.set, "__func__", None) is not RedisCache.set
        ):
            return None
        for name in (
            "_get_revocation_state",
            "get_refresh_state",
            "is_revoked",
            "is_family_revoked",
            "get_user_version",
            "revoke_family",
        ):
            if getattr(getattr(self, name), "__func__", None) is not getattr(DjangoCacheRevocation, name):
                return None
        if not jti or not fam or type(expected_version) is not int or expected_version < 0:
            raise ValueError("Invalid atomic JWT logout input")
        ttl = _ttl_for(exp, self.default_ttl)
        if ttl <= 0:
            raise ValueError("Atomic JWT logout requires positive marker TTL")

        keys = [
            cache.make_and_validate_key(f"{self.key_prefix}{jti}"),
            cache.make_and_validate_key(f"{self.key_prefix}fam:{fam}"),
            cache.make_and_validate_key(f"{self.key_prefix}ver:{user_id}"),
        ]
        backend = cache._cache
        client = backend.get_client(keys[0], write=True)
        script = getattr(self, "_jwt_logout_script", None)
        if script is None:
            script = client.register_script(_REDIS_REVOKE_ACTIVE_FAMILY)
            self._jwt_logout_script = script
        # RedisCache.set() pickles strings. A raw '1' would violate that contract.
        marker = backend._serializer.dumps("1")
        # redis-py Script handles NOSCRIPT by loading and retrying after failover.
        return bool(script(keys=keys, args=[str(expected_version), marker, ttl], client=client))

    async def revoke_family(self, fam: str, *, exp: int | None = None) -> None:
        key = f"{self.key_prefix}fam:{fam}"
        self.cache.set(key, "1", timeout=_ttl_for(exp, self.default_ttl))

    async def is_family_revoked(self, fam: str) -> bool:
        return self._get_revocation_state(f"{self.key_prefix}fam:{fam}") is not None


class DjangoORMRevocation(RevocationStore):
    """
    Database-based revocation store using Django ORM.

    ⚠️  Slower than cache-based solutions (~1k-5k ops/sec vs 50k ops/sec).
    Only use if you don't have cache infrastructure.

    Requires creating a model:
    ```python
    # myapp/models.py
    from django.db import models

    class RevokedToken(models.Model):
        jti = models.CharField(max_length=255, unique=True, db_index=True)
        revoked_at = models.DateTimeField(auto_now_add=True)
        expires_at = models.DateTimeField(db_index=True)

        class Meta:
            indexes = [
                models.Index(fields=['jti']),
                models.Index(fields=['expires_at']),
            ]
    ```

    Then run migrations:
    ```bash
    python manage.py makemigrations
    python manage.py migrate
    ```

    Usage:
    ```python
    from django_bolt.auth.revocation import DjangoORMRevocation

    revocation = DjangoORMRevocation(model='myapp.RevokedToken')

    auth = JWTAuthentication(
        secret=settings.SECRET_KEY,
        revocation_store=revocation,
    )

    # In logout endpoint — pass `exp` so the row's expires_at aligns
    # with the token's own expiry.
    @api.post("/logout")
    async def logout(request):
        claims = request["context"]["auth_claims"]
        await revocation.revoke(claims["jti"], exp=claims["exp"] + auth.leeway)
        return {"message": "Logged out"}
    ```

    ⚠️  Remember to add a cleanup task to delete expired tokens:
    ```python
    # Periodic task (celery, cron, etc.)
    from datetime import datetime, timezone

    async def cleanup_expired_tokens():
        await RevokedToken.objects.filter(
            expires_at__lt=datetime.now(timezone.utc)
        ).adelete()
    ```
    """

    def __init__(self, model: str, *, default_ttl: int = _DEFAULT_TTL_SECONDS):
        """
        Initialize ORM-based revocation.

        Args:
            model: Two-part model path ``'app_label.ModelName'``
                (e.g., ``'myapp.RevokedToken'``).
            default_ttl: Fallback retention (in seconds) used when a caller
                invokes ``revoke(jti)`` without passing ``exp``. Defaults to
                7 days. Per-call ``exp`` always wins over this.
        """
        self.model_path = model
        self.default_ttl = default_ttl
        self._model = None

    @property
    def model(self):
        """Lazy-load model to avoid import issues."""
        if self._model is None:
            parts = self.model_path.split(".")
            if len(parts) != 2 or not all(parts):
                raise ValueError(f"model must be 'app_label.ModelName', got '{self.model_path}'")
            app_label, model_name = parts
            self._model = apps.get_model(app_label, model_name)
        return self._model

    async def is_revoked(self, jti: str) -> bool:
        return await self.model.objects.filter(jti=jti).aexists()

    async def revoke(self, jti: str, *, exp: int | None = None) -> None:
        """
        Revoke a token by upserting a row keyed on its JTI.

        Pass an absolute retention deadline that includes the JWT
        verifier's leeway; cleanup must not remove a row while the JWT can
        still be accepted.

            await store.revoke(claims["jti"], exp=claims["exp"] + auth.leeway)

        If ``exp`` is omitted, ``expires_at`` is set to ``now +
        self.default_ttl`` (set on the constructor).
        """
        expires_at = datetime.now(UTC) + timedelta(seconds=_ttl_for(exp, self.default_ttl))
        await self.model.objects.aupdate_or_create(jti=jti, defaults={"expires_at": expires_at})

    async def consume(self, jti: str, *, exp: int | None = None) -> bool:
        expires_at = datetime.now(UTC) + timedelta(seconds=_ttl_for(exp, self.default_ttl))
        try:
            await self.model.objects.acreate(jti=jti, expires_at=expires_at)
        except IntegrityError:
            return False
        return True

    async def revoke_family(self, fam: str, *, exp: int | None = None) -> None:
        await self.revoke(f"family:{fam}", exp=exp)

    async def is_family_revoked(self, fam: str) -> bool:
        return await self.is_revoked(f"family:{fam}")


def create_revocation_handler(store: RevocationStore):
    """
    Create a revoked_token_handler from a RevocationStore.

    This is a convenience function to convert a RevocationStore into
    a callable that can be passed to JWTAuthentication.

    Args:
        store: RevocationStore instance

    Returns:
        Async callable that checks if token is revoked

    Example:
        ```python
        from django_bolt.auth.revocation import InMemoryRevocation, create_revocation_handler

        store = InMemoryRevocation()
        handler = create_revocation_handler(store)

        auth = JWTAuthentication(
            secret=settings.SECRET_KEY,
            revoked_token_handler=handler,
        )
        ```
    """

    async def handler(jti: str) -> bool:
        return await store.is_revoked(jti)

    return handler
