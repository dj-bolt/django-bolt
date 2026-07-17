"""
Token revocation support for Django-Bolt.

Provides flexible revocation strategies that users can choose based on their needs.
Revocation is OPTIONAL - only checked if user provides a handler.
"""

import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta

from django.apps import apps
from django.core.cache import caches

_DEFAULT_TTL_SECONDS = 86400 * 7  # 7 days — fallback when caller doesn't pass `exp`


def _ttl_for(exp: int | None, default: int = _DEFAULT_TTL_SECONDS) -> int:
    """
    Seconds a revocation entry should live.

    Prefers the token's own ``exp`` (so the entry expires exactly when the
    token would have anyway). A past ``exp`` clamps to 0 — we never hand a
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

        The revocation entry must outlive the token, otherwise an attacker
        could replay a still-valid JWT after the entry is cleaned up. Pass
        the token's ``exp`` claim so the entry expires exactly when the
        token would have anyway:

            await store.revoke(claims["jti"], exp=claims.get("exp"))

        Args:
            jti: JWT ID to revoke.
            exp: The token's ``exp`` claim (epoch seconds). Strongly
                recommended. If omitted, falls back to a 30-day retention —
                safe for most refresh-token windows but wasteful for short
                access tokens.
        """
        pass

    async def revoke_all(self, user_id: str) -> None:
        """
        Revoke all tokens for a user (optional, not all stores support this).

        Args:
            user_id: User identifier
        """
        raise NotImplementedError("This revocation store does not support revoke_all")

    # --- Bulk primitives (optional) -------------------------------------
    # Per-``jti`` revocation only kills a single session. Two production
    # flows need more: "log out everywhere" (invalidate every outstanding
    # token for a user) and refresh-token reuse detection (kill an entire
    # rotation family when a rotated-out token is replayed). These are
    # expressed with a per-user token *version* and a per-family revoke,
    # both O(1) and TTL-cleaned — no key scanning required.

    async def get_user_version(self, user_id: str) -> int:
        """Current token version for a user (0 if never bumped).

        A token carrying ``ver`` less than this value is stale and must be
        rejected. Used to implement global logout without enumerating jtis.
        """
        raise NotImplementedError("This revocation store does not support token versioning")

    async def bump_user_version(self, user_id: str) -> int:
        """Invalidate every outstanding token for a user (global logout).

        Increments and returns the user's token version. Tokens minted
        before the bump carry a lower ``ver`` and are rejected.
        """
        raise NotImplementedError("This revocation store does not support token versioning")

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
            await revocation.revoke(claims["jti"], exp=claims.get("exp"))
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

    async def get_user_version(self, user_id: str) -> int:
        return self._user_versions.get(str(user_id), 0)

    async def bump_user_version(self, user_id: str) -> int:
        new_version = self._user_versions.get(str(user_id), 0) + 1
        self._user_versions[str(user_id)] = new_version
        return new_version

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

    Works with ANY Django cache backend:
    - Redis (django.core.cache.backends.redis.RedisCache)
    - Memcached (django.core.cache.backends.memcached.PyMemcacheCache)
    - Database (django.core.cache.backends.db.DatabaseCache)
    - File-based (django.core.cache.backends.filebased.FileBasedCache)
    - Local memory (django.core.cache.backends.locmem.LocMemCache)

    ✅ Production-ready: Works across multiple processes/workers
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
            await revocation.revoke(claims["jti"], exp=claims.get("exp"))
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

    async def is_revoked(self, jti: str) -> bool:
        key = f"{self.key_prefix}{jti}"
        # Django cache get is sync, but fast
        return self.cache.get(key) is not None

    async def revoke(self, jti: str, *, exp: int | None = None) -> None:
        """
        Revoke a token by storing its JTI in the cache.

        Pass ``exp`` (the token's ``exp`` claim) so the cache entry expires
        exactly when the token would have — no security gap, no wasted
        storage.

            await store.revoke(claims["jti"], exp=claims.get("exp"))

        If ``exp`` is omitted, the entry lives for ``self.default_ttl``
        seconds (set on the constructor).
        """
        key = f"{self.key_prefix}{jti}"
        self.cache.set(key, "1", timeout=_ttl_for(exp, self.default_ttl))

    async def get_user_version(self, user_id: str) -> int:
        return int(self.cache.get(f"{self.key_prefix}ver:{user_id}") or 0)

    async def bump_user_version(self, user_id: str) -> int:
        # User-version keys never expire — they are the source of truth for
        # "log out everywhere" and must outlive any token they invalidate.
        key = f"{self.key_prefix}ver:{user_id}"
        try:
            return self.cache.incr(key)
        except ValueError:
            # Key absent: seed it. (Django's cache.incr raises if missing.)
            self.cache.set(key, 1, timeout=None)
            return 1

    async def revoke_family(self, fam: str, *, exp: int | None = None) -> None:
        key = f"{self.key_prefix}fam:{fam}"
        self.cache.set(key, "1", timeout=_ttl_for(exp, self.default_ttl))

    async def is_family_revoked(self, fam: str) -> bool:
        return self.cache.get(f"{self.key_prefix}fam:{fam}") is not None


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
        await revocation.revoke(claims["jti"], exp=claims.get("exp"))
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

        Pass ``exp`` (the token's ``exp`` claim) so ``expires_at`` aligns
        with the token's own expiry — your cleanup task can drop the row
        exactly when the token would have stopped being honored anyway.

            await store.revoke(claims["jti"], exp=claims.get("exp"))

        If ``exp`` is omitted, ``expires_at`` is set to ``now +
        self.default_ttl`` (set on the constructor).
        """
        expires_at = datetime.now(UTC) + timedelta(seconds=_ttl_for(exp, self.default_ttl))
        await self.model.objects.aupdate_or_create(jti=jti, defaults={"expires_at": expires_at})


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
