"""
User loading system for request.user

Supports both eager and lazy loading strategies.
Lazy loading (default): Uses SimpleLazyObject to defer DB query until first access
Eager loading (optional): Loads user immediately at dispatch time

Backends customize user loading by overriding get_user (async) and/or
get_user_sync (sync) — overriding either one alone is honored. The effective
strategy is resolved once per route at registration time (resolve_user_loader,
stored in the handler meta keyed by scheme name), so the per-request path is a
single dict lookup plus a call. Resolution must be per-route, not per scheme
name: two JWTAuthentication subclasses share scheme_name "jwt" but can carry
different get_user overrides.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from django.contrib.auth import get_user_model

from ..concurrency import _run_default_blocking, run_in_orm_executor, run_orm_blocking
from .backends import BaseAuthentication, JWTAuthentication
from .pk_loader import load_user_by_pk_sync

__all__ = [
    "load_user_by_pk_sync",
    "register_auth_backend",
    "get_registered_backend",
    "resolve_user_loader",
    "load_user",
    "load_user_sync",
    "default_django_user_loader",
]

# Framework-provided implementations. A backend method that is one of these
# was NOT overridden by the user — only genuine overrides take priority.
_FRAMEWORK_GET_USER = (BaseAuthentication.get_user, JWTAuthentication.get_user)
_FRAMEWORK_GET_USER_SYNC = (JWTAuthentication.get_user_sync,)

# Global registry of auth backend instances for user resolution
_auth_backend_registry: dict[str, Any] = {}

# (backend_name) -> resolved loader (user_id, auth_context, is_async_context) -> user,
# or None when the backend has no user resolution. Built at registration time.
_resolved_loader_registry: dict[str, Callable[[str, dict | None, bool], Any] | None] = {}


def _has_custom_get_user_sync(cls: type) -> bool:
    method = getattr(cls, "get_user_sync", None)
    return method is not None and method not in _FRAMEWORK_GET_USER_SYNC


def _has_custom_get_user(cls: type) -> bool:
    method = getattr(cls, "get_user", None)
    return method is not None and method not in _FRAMEWORK_GET_USER


def resolve_user_loader(backend: Any) -> Callable[[str, dict | None, bool], Any] | None:
    """
    Resolve the sync user-loading strategy for a backend, once at registration.

    Priority:
    1. Overridden get_user_sync (fastest — direct call, no event loop)
    2. Overridden get_user (async or sync)
    3. Framework default get_user_sync (JWTAuthentication pk lookup)
    4. None — backend has no user resolution (e.g. plain APIKeyAuthentication)
    """
    cls = type(backend)
    custom_sync = _has_custom_get_user_sync(cls)
    custom_async = _has_custom_get_user(cls)
    has_framework_sync = getattr(cls, "get_user_sync", None) is not None

    if custom_sync or (has_framework_sync and not custom_async):

        def load_via_sync(user_id: str, auth_context: dict | None, is_async_context: bool) -> Any:
            if is_async_context:
                return run_orm_blocking(backend.get_user_sync, user_id)
            return backend.get_user_sync(user_id)

        return load_via_sync

    if custom_async:
        get_user = backend.get_user

        if not inspect.iscoroutinefunction(get_user):

            def load_via_plain(user_id: str, auth_context: dict | None, is_async_context: bool) -> Any:
                if is_async_context:
                    return run_orm_blocking(get_user, user_id, auth_context or {})
                return get_user(user_id, auth_context or {})

            return load_via_plain

        def load_via_async(user_id: str, auth_context: dict | None, is_async_context: bool) -> Any:
            # asyncio.run needs a thread without a running loop — always cross,
            # regardless of handler context. The shim goes to the generic
            # default pool, not the ORM pool: the coroutine may await
            # non-database work (an external identity provider), and parking
            # its whole lifetime on a bounded ORM slot would queue every
            # QuerySet evaluation behind it. The database work the coroutine
            # awaits still reaches the bounded pool through its own hand-off
            # (run_in_orm_executor, or the async ORM's executor).
            def run_async_get_user():
                return asyncio.run(get_user(user_id, auth_context or {}))

            return _run_default_blocking(run_async_get_user)

        return load_via_async

    return None


def default_django_user_loader(user_id: str, auth_context: dict | None, is_async_context: bool) -> Any:
    """Default user query for schemes with no backend instance on the route
    (e.g. Rust-side session auth), via the pre-compiled pk query.

    Returns None for a stale user_id (user deleted after the session/token
    was issued), mirroring the framework get_user/get_user_sync defaults.
    """
    User = get_user_model()

    if is_async_context:
        return run_orm_blocking(load_user_by_pk_sync, User, user_id)
    return load_user_by_pk_sync(User, user_id)


def register_auth_backend(backend_name: str, backend_instance: Any) -> None:
    """
    Register an authentication backend instance for user resolution.

    Called at server startup to make backends available for user loading.
    Resolves the user-loading strategy once, so request-time lookup is O(1).

    Args:
        backend_name: Unique identifier for the backend (e.g., "jwt", "api_key")
        backend_instance: Instance of the authentication backend class
    """
    _auth_backend_registry[backend_name] = backend_instance
    _resolved_loader_registry[backend_name] = resolve_user_loader(backend_instance)


def get_registered_backend(backend_name: str) -> Any | None:
    """Get a registered auth backend by name."""
    return _auth_backend_registry.get(backend_name)


async def load_user(user_id: str | None, backend_name: str | None, auth_context: dict | None = None) -> Any | None:
    """
    Eagerly load user from auth context.

    Loads user immediately (not lazy). Suitable for authenticated endpoints
    where user is always needed.

    Args:
        user_id: User identifier from auth context
        backend_name: Authentication backend name (e.g., "jwt", "api_key")
        auth_context: Full authentication context dict

    Returns:
        User object, or None if not found or no user_id
    """
    if not user_id:
        return None

    backend = _auth_backend_registry.get(backend_name) if backend_name else None
    if backend is None:
        return None

    cls = type(backend)
    if _has_custom_get_user_sync(cls) and not _has_custom_get_user(cls):
        # Only the sync variant was customized — run it off the event loop, on
        # the ORM pool rather than the generic default one: it is a query, and
        # its connection use belongs to the same budget as every other query.
        return await run_in_orm_executor(backend.get_user_sync, user_id)

    return await backend.get_user(user_id, auth_context or {})


def load_user_sync(
    user_id: str | None,
    backend_name: str | None,
    auth_context: dict | None = None,
    is_async_context: bool = False,
) -> Any | None:
    """
    Synchronously load user from auth context.

    This is the sync version used by SimpleLazyObject for lazy loading.
    Handles thread pool wrapping for async contexts.

    Args:
        user_id: User identifier from auth context
        backend_name: Authentication backend name (e.g., "jwt", "api_key")
        auth_context: Full authentication context dict
        is_async_context: Whether we're in an async handler (use thread pool)

    Returns:
        User object, or None if not found or no user_id
    """
    if not user_id:
        return None

    if backend_name in _auth_backend_registry:
        loader = _resolved_loader_registry[backend_name]
        if loader is None:
            # Backend provides no user resolution (e.g. plain APIKeyAuthentication)
            return None
        return loader(user_id, auth_context, is_async_context)

    # Unregistered backend (e.g. session auth): default Django user query
    return default_django_user_loader(user_id, auth_context, is_async_context)
