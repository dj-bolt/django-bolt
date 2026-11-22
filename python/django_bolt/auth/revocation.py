"""
Token revocation support for Django-Bolt.

Provides flexible revocation strategies that users can choose based on their needs.
Revocation is OPTIONAL - only checked if user provides a handler.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta

from django.apps import apps
from django.core.cache import caches
from django.core.cache.backends.base import BaseCache, InvalidCacheKey
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.db.models import F

_DEFAULT_TTL_SECONDS = 86400 * 7  # 7 days — fallback when caller doesn't pass `exp`
# A user version row must stay, so the cleanup task of expired rows must not find it.
_NEVER_EXPIRES = datetime(9999, 1, 1, tzinfo=UTC)
# The key of a family or of a user version starts with one of these prefixes.
# A jti with such a prefix would read or write that key, so the stores refuse it.
_CACHE_RESERVED_PREFIXES = ("fam:", "ver:")
_ORM_RESERVED_PREFIXES = ("family:", "version:")


class ReservedJtiError(ValueError):
    """A store refuses a ``jti``, a family or a user id that it cannot keep.

    A ``jti`` that starts with a reserved prefix gets this error. A key that the
    storage cannot keep also gets it, for example a key longer than the column.
    Authentication and ``JWTAuthViews`` count such a token as revoked.
    """


def _check_jti(jti: str, reserved: tuple[str, ...]) -> None:
    """Raise ``ReservedJtiError`` when ``jti`` starts with a reserved prefix."""
    if jti.startswith(reserved):
        raise ReservedJtiError(
            f"jti {jti!r} starts with a reserved prefix ({', '.join(reserved)}). "
            "The store uses these prefixes for families and user versions."
        )


def _refused_cache_key(error: InvalidCacheKey) -> ReservedJtiError:
    """Return the error for a key that the cache backend refuses, for example memcached."""
    return ReservedJtiError(f"The cache backend cannot keep this key: {error}")


def _ttl_for(exp: int | None, default: int = _DEFAULT_TTL_SECONDS, *, leeway: int) -> int:
    """
    Return the seconds that a revocation entry must live.

    With ``exp``, the entry lives until the end of the second ``exp + leeway``.
    ``JWTAuthentication`` accepts a token until the end of that second, so the
    entry must not expire before it. A token after that second gets 0, not a
    negative value, because ``timedelta`` subtracts a negative duration from
    now. Without ``exp``, the result is ``default``, usually the
    ``default_ttl`` of the store. ``leeway`` does not change ``default``.
    """
    if exp is None:
        return default
    return max(0, int(exp) + leeway - int(time.time()) + 1)


def _checked_leeway(store_name: str, leeway: int) -> int:
    """Return ``leeway``. Raise ``ImproperlyConfigured`` when it is negative."""
    if leeway < 0:
        raise ImproperlyConfigured(f"{store_name} leeway must be >= 0 seconds.")
    return leeway


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
        could replay a still-valid JWT after the entry is cleaned up.
        ``JWTAuthentication`` accepts a token until ``exp + leeway``, so the
        entry must live until that time. Pass the token's ``exp`` claim:

            await store.revoke(claims["jti"], exp=claims.get("exp"))

        Args:
            jti: JWT ID to revoke.
            exp: The token's ``exp`` claim (epoch seconds). Strongly
                recommended. If omitted, the stores of Bolt keep the entry
                for their ``default_ttl`` (7 days by default). That is safe
                only for a token that expires before then, and wasteful for
                short access tokens.
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
        # when the backend stops accepting the token (`exp + leeway`).
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
        leeway: int = 60,
    ):
        """
        Initialize Django cache-based revocation.

        Args:
            cache_alias: Django cache alias to use (default: 'default').
            key_prefix: Prefix for cache keys (default: 'revoked:').
            default_ttl: Fallback retention (in seconds) used when a caller
                invokes ``revoke(jti)`` without passing ``exp``. Defaults to
                7 days. Per-call ``exp`` always wins over this.
            leeway: The ``leeway`` of the ``JWTAuthentication`` that reads
                this store (default: 60). An entry lives until
                ``exp + leeway``.
        """
        self.cache_alias = cache_alias
        self.key_prefix = key_prefix
        self.default_ttl = default_ttl
        self.leeway = _checked_leeway("DjangoCacheRevocation", leeway)
        self._cache = None

    @property
    def cache(self):
        """Lazy-load cache to avoid import issues."""
        if self._cache is None:
            self._cache = caches[self.cache_alias]
        return self._cache

    async def is_revoked(self, jti: str) -> bool:
        _check_jti(jti, _CACHE_RESERVED_PREFIXES)
        key = f"{self.key_prefix}{jti}"
        # Django cache get is sync, but fast. A memcached backend refuses a key with a space
        # or a key over 250 characters.
        try:
            return self.cache.get(key) is not None
        except InvalidCacheKey as exc:
            raise _refused_cache_key(exc) from exc

    async def revoke(self, jti: str, *, exp: int | None = None) -> None:
        """
        Revoke a token by storing its JTI in the cache.

        Pass ``exp`` (the token's ``exp`` claim). The cache entry then
        lives until ``exp + self.leeway``, the time when the backend stops
        accepting the token.

            await store.revoke(claims["jti"], exp=claims.get("exp"))

        If ``exp`` is omitted, the entry lives for ``self.default_ttl``
        seconds (set on the constructor).

        A ``jti`` that starts with ``fam:`` or ``ver:`` raises ``ReservedJtiError``.
        A key that the cache backend refuses also raises ``ReservedJtiError``.
        """
        _check_jti(jti, _CACHE_RESERVED_PREFIXES)
        key = f"{self.key_prefix}{jti}"
        try:
            self.cache.set(key, "1", timeout=_ttl_for(exp, self.default_ttl, leeway=self.leeway))
        except InvalidCacheKey as exc:
            raise _refused_cache_key(exc) from exc

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
        _check_jti(jti, _CACHE_RESERVED_PREFIXES)
        key = f"{self.key_prefix}{jti}"
        ttl = _ttl_for(exp, self.default_ttl, leeway=self.leeway)
        if ttl == 0:
            # Django deletes an entry with a timeout of 0, so its marker cannot block a replay.
            return False
        try:
            return bool(self.cache.add(key, "1", timeout=ttl))
        except InvalidCacheKey as exc:
            raise _refused_cache_key(exc) from exc

    async def get_user_version(self, user_id: str) -> int:
        try:
            return int(self.cache.get(f"{self.key_prefix}ver:{user_id}") or 0)
        except InvalidCacheKey as exc:
            raise _refused_cache_key(exc) from exc

    async def bump_user_version(self, user_id: str) -> int:
        # User-version keys never expire — they are the source of truth for
        # "log out everywhere" and must outlive any token they invalidate.
        self._require_atomic_cache()
        key = f"{self.key_prefix}ver:{user_id}"
        try:
            return self.cache.incr(key)
        except InvalidCacheKey as exc:
            # InvalidCacheKey is a ValueError, but the key is not missing: the backend refuses it.
            raise _refused_cache_key(exc) from exc
        except ValueError:
            # Seed atomically. If another caller won the add race, increment
            # its value so no concurrent logout event is lost.
            if self.cache.add(key, 1, timeout=None):
                return 1
            return self.cache.incr(key)

    async def revoke_family(self, fam: str, *, exp: int | None = None) -> None:
        key = f"{self.key_prefix}fam:{fam}"
        try:
            self.cache.set(key, "1", timeout=_ttl_for(exp, self.default_ttl, leeway=self.leeway))
        except InvalidCacheKey as exc:
            raise _refused_cache_key(exc) from exc

    async def is_family_revoked(self, fam: str) -> bool:
        try:
            return self.cache.get(f"{self.key_prefix}fam:{fam}") is not None
        except InvalidCacheKey as exc:
            raise _refused_cache_key(exc) from exc


class DjangoORMRevocation(RevocationStore):
    """
    Database-based revocation store using Django ORM.

    ⚠️  Slower than cache-based solutions (~1k-5k ops/sec vs 50k ops/sec).
    Only use if you don't have cache infrastructure.

    Add the app that has the table, then run ``python manage.py migrate``:
    ```python
    INSTALLED_APPS = [
        ...,
        "django_bolt.auth.revocation_db",
    ]
    ```

    The store keeps the version of a user in the row ``version:<user_id>``.
    That row does not expire, so the cleanup task below keeps it.

    To use a model of your own, pass ``model="myapp.MyModel"``. The model must have
    the fields of ``django_bolt.auth.revocation_db.models.RevokedToken``.

    Usage:
    ```python
    from django_bolt.auth.revocation import DjangoORMRevocation

    revocation = DjangoORMRevocation()

    auth = JWTAuthentication(
        secret=settings.SECRET_KEY,
        revocation_store=revocation,
    )

    # In logout endpoint — pass `exp` so the row's expires_at is the time
    # when the backend stops accepting the token (`exp + leeway`).
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

    from django_bolt.auth.revocation_db.models import RevokedToken

    async def cleanup_expired_tokens():
        await RevokedToken.objects.filter(
            expires_at__lt=datetime.now(timezone.utc)
        ).adelete()
    ```
    """

    def __init__(
        self,
        model: str = "bolt_revocation.RevokedToken",
        *,
        default_ttl: int = _DEFAULT_TTL_SECONDS,
        leeway: int = 60,
    ):
        """
        Initialize ORM-based revocation.

        Args:
            model: Two-part model path ``'app_label.ModelName'``. The default is the
                model of ``django_bolt.auth.revocation_db``.
            default_ttl: Fallback retention (in seconds) used when a caller
                invokes ``revoke(jti)`` without passing ``exp``. Defaults to
                7 days. Per-call ``exp`` always wins over this.
            leeway: The ``leeway`` of the ``JWTAuthentication`` that reads
                this store (default: 60). An entry lives until
                ``exp + leeway``.
        """
        self.model_path = model
        self.default_ttl = default_ttl
        self.leeway = _checked_leeway("DjangoORMRevocation", leeway)
        self._model = None
        # The max_length of the jti field of the model. It is None until the model loads.
        self._max_key_length: int | None = None

    @property
    def model(self):
        """Lazy-load model to avoid import issues."""
        if self._model is None:
            parts = self.model_path.split(".")
            if len(parts) != 2 or not all(parts):
                raise ValueError(f"model must be 'app_label.ModelName', got '{self.model_path}'")
            app_label, model_name = parts
            model = apps.get_model(app_label, model_name)
            self._max_key_length = model._meta.get_field("jti").max_length
            self._model = model
        return self._model

    def _model_for(self, key: str):
        """Return the model. Raise ``ReservedJtiError`` when its ``jti`` column cannot keep ``key``.

        PostgreSQL raises ``DataError`` for a key that is too long or that has a NUL
        character. MySQL without strict mode cuts a long key short, so two keys can
        share one row. Thus the store refuses such a key before the query.
        """
        model = self.model
        limit = self._max_key_length
        if limit is not None and len(key) > limit:
            raise ReservedJtiError(f"The key has {len(key)} characters. The jti column keeps at most {limit}.")
        if "\x00" in key:
            raise ReservedJtiError("The key has a NUL character. The jti column cannot keep it.")
        return model

    async def is_revoked(self, jti: str) -> bool:
        _check_jti(jti, _ORM_RESERVED_PREFIXES)
        return await self._model_for(jti).objects.filter(jti=jti).aexists()

    async def revoke(self, jti: str, *, exp: int | None = None) -> None:
        """
        Revoke a token by upserting a row keyed on its JTI.

        Pass ``exp`` (the token's ``exp`` claim). ``expires_at`` is then
        ``exp + self.leeway``, the time when the backend stops accepting the
        token. Your cleanup task can delete the row after that time.

            await store.revoke(claims["jti"], exp=claims.get("exp"))

        If ``exp`` is omitted, ``expires_at`` is set to ``now +
        self.default_ttl`` (set on the constructor).

        A ``jti`` that starts with ``family:`` or ``version:`` raises ``ReservedJtiError``.
        A ``jti`` that the ``jti`` column cannot keep also raises ``ReservedJtiError``.
        """
        _check_jti(jti, _ORM_RESERVED_PREFIXES)
        await self._upsert(jti, exp)

    async def _upsert(self, jti: str, exp: int | None) -> None:
        # Read the TTL first: expires_at is then never before the end of exp + leeway.
        ttl = _ttl_for(exp, self.default_ttl, leeway=self.leeway)
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        await self._model_for(jti).objects.aupdate_or_create(jti=jti, defaults={"expires_at": expires_at})

    async def consume(self, jti: str, *, exp: int | None = None) -> bool:
        _check_jti(jti, _ORM_RESERVED_PREFIXES)
        model = self._model_for(jti)
        ttl = _ttl_for(exp, self.default_ttl, leeway=self.leeway)
        if ttl == 0:
            # The cleanup task deletes an expired row, and a later consume would then
            # succeed. The backend refuses this token, so refuse it here too.
            return False
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        try:
            await model.objects.acreate(jti=jti, expires_at=expires_at)
        except IntegrityError:
            return False
        return True

    async def revoke_family(self, fam: str, *, exp: int | None = None) -> None:
        await self._upsert(f"family:{fam}", exp)

    async def is_family_revoked(self, fam: str) -> bool:
        key = f"family:{fam}"
        return await self._model_for(key).objects.filter(jti=key).aexists()

    async def get_user_version(self, user_id: str) -> int:
        key = f"version:{user_id}"
        rows = self._model_for(key).objects.filter(jti=key)
        return await rows.values_list("version", flat=True).afirst() or 0

    async def bump_user_version(self, user_id: str) -> int:
        """Add 1 to the user version in one ``UPDATE``, so concurrent bumps do not lose a count.

        A concurrent bump can make the returned version larger than this bump alone.
        """
        key = f"version:{user_id}"
        model = self._model_for(key)
        await model.objects.aget_or_create(jti=key, defaults={"expires_at": _NEVER_EXPIRES})
        await model.objects.filter(jti=key).aupdate(version=F("version") + 1)
        return await self.get_user_version(user_id)


def create_revocation_handler(store: RevocationStore):
    """
    Create a revoked_token_handler from a RevocationStore.

    This is a convenience function to convert a RevocationStore into
    a callable that can be passed to JWTAuthentication.

    Args:
        store: RevocationStore instance

    Returns:
        Async callable that checks if token is revoked. A ``ReservedJtiError``
        from ``store.is_revoked`` gives ``True``, so the backend refuses the token.

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
        try:
            return await store.is_revoked(jti)
        except ReservedJtiError:
            # The store refuses this jti, for example a jti with a reserved prefix.
            # The store cannot check such a token, so the token counts as revoked.
            return True

    return handler
