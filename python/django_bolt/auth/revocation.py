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

    async def is_revoked(self, jti: str) -> bool:
        return jti in self._revoked

    async def revoke(self, jti: str, *, exp: int | None = None) -> None:
        self._revoked.add(jti)

    def clear(self) -> None:
        """Clear all revoked tokens (useful for testing)."""
        self._revoked.clear()


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
