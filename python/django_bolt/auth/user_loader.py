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

Each backend resolves to a pair of loaders:

1. request.user is sync. It runs the query on the thread that reads it
   (run_orm_blocking). A thread with a running event loop sends the query to
   the ORM pool, and the loop waits. On the loop of a request with Django
   middleware, the query would miss the state of the lane, so the read raises.
2. await request.auser() is async. It awaits an async get_user as a coroutine.
   It sends a sync query through run_in_orm_executor, the hand-off of each
   framework query. A request with a lane keeps the query on that lane.
3. A backend with only an async get_user serves request.auser() only. Sync
   access to request.user raises and names the fix.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any

from django.contrib.auth import get_user_model
from django.utils.functional import SimpleLazyObject, empty

from ..concurrency import run_in_orm_executor, run_orm_blocking
from .backends import BaseAuthentication, JWTAuthentication
from .pk_loader import load_user_by_pk_sync

__all__ = [
    "LazyUser",
    "aload_bolt_user",
    "load_user_by_pk_sync",
    "register_auth_backend",
    "get_registered_backend",
    "resolve_user_loader",
    "load_user",
    "load_user_sync",
    "default_django_user_loader",
    "default_django_user_aloader",
]

SyncLoader = Callable[[str, dict | None], Any]
AsyncLoader = Callable[[str, dict | None], Coroutine[Any, Any, Any]]
UserLoaders = tuple[SyncLoader, AsyncLoader]

# Framework-provided implementations. A backend method that is one of these
# was NOT overridden by the user — only genuine overrides take priority.
_FRAMEWORK_GET_USER = (BaseAuthentication.get_user, JWTAuthentication.get_user)
_FRAMEWORK_GET_USER_SYNC = (JWTAuthentication.get_user_sync,)

# Global registry of auth backend instances for user resolution
_auth_backend_registry: dict[str, Any] = {}

# (backend_name) -> resolved (sync loader, async loader), each (user_id, auth_context) -> user,
# or None when the backend has no user resolution. Built at registration time.
_resolved_loader_registry: dict[str, UserLoaders | None] = {}


class LazyUser(SimpleLazyObject):
    """``request.user`` of a request that Bolt authenticated.

    Sync access forces the user on the calling thread, as ``SimpleLazyObject``
    does. ``await request.auser()`` calls :meth:`aload`, which awaits the
    async loader and does not block the event loop. Both share one result.
    """

    def __init__(self, loaders: UserLoaders, user_id: str, auth_context: dict | None) -> None:
        # Set the state of SimpleLazyObject.__init__ directly. Each request with a
        # user builds one, and the chain of __init__ calls costs more than the writes.
        # LazyObject.__setattr__ forwards other names to the wrapped user.
        state = self.__dict__
        state["_setupfunc"] = partial(loaders[0], user_id, auth_context)
        state["_wrapped"] = empty
        state["_aloader"] = loaders[1]

    async def aload(self) -> Any:
        if self._wrapped is empty:
            state = self.__dict__
            lock = state.get("_aload_lock")
            if lock is None:
                lock = state["_aload_lock"] = asyncio.Lock()
            # Concurrent calls of one request share one query.
            async with lock:
                if self._wrapped is empty:
                    user = await self._aloader(*self._setupfunc.args)
                    # A sync read can load the user during the await. Keep that user,
                    # so request.user does not change in the middle of the request.
                    if self._wrapped is empty:
                        self._wrapped = user
        return self._wrapped


async def aload_bolt_user(user: Any) -> Any:
    """The result of ``await request.auser()`` for a user that Bolt set on the request."""
    # ``isinstance`` on a lazy object forces it. ``type`` does not.
    if type(user) is LazyUser:
        return await user.aload()
    return user


def _has_custom_get_user_sync(cls: type) -> bool:
    method = getattr(cls, "get_user_sync", None)
    return method is not None and method not in _FRAMEWORK_GET_USER_SYNC


def _has_custom_get_user(cls: type) -> bool:
    method = getattr(cls, "get_user", None)
    return method is not None and method not in _FRAMEWORK_GET_USER


def resolve_user_loader(backend: Any) -> UserLoaders | None:
    """
    Resolve the user-loading strategy for a backend, once at registration.

    Priority:
    1. Overridden get_user_sync (fastest — direct call, no event loop)
    2. Overridden get_user (async or sync)
    3. Framework default get_user_sync (JWTAuthentication pk lookup)
    4. None — backend has no user resolution (e.g. plain APIKeyAuthentication)

    Returns the sync loader of ``request.user`` and the async loader of
    ``await request.auser()``.
    """
    cls = type(backend)
    custom_sync = _has_custom_get_user_sync(cls)
    custom_async = _has_custom_get_user(cls)
    has_framework_sync = getattr(cls, "get_user_sync", None) is not None

    if custom_sync or (has_framework_sync and not custom_async):
        get_user_sync = backend.get_user_sync

        def load_via_sync(user_id: str, auth_context: dict | None) -> Any:
            return run_orm_blocking(get_user_sync, user_id)

        async def aload_via_sync(user_id: str, auth_context: dict | None) -> Any:
            return await run_in_orm_executor(get_user_sync, user_id)

        return load_via_sync, aload_via_sync

    if custom_async:
        get_user = backend.get_user

        if not inspect.iscoroutinefunction(get_user):

            def load_via_plain(user_id: str, auth_context: dict | None) -> Any:
                return run_orm_blocking(get_user, user_id, auth_context or {})

            async def aload_via_plain(user_id: str, auth_context: dict | None) -> Any:
                return await run_in_orm_executor(get_user, user_id, auth_context or {})

            return load_via_plain, aload_via_plain

        def reject_sync_access(user_id: str, auth_context: dict | None) -> Any:
            raise RuntimeError(
                f"{cls.__qualname__}.get_user is async, so sync access to request.user cannot run it. "
                "Use `await request.auser()`, or define get_user_sync on the backend."
            )

        async def aload_via_async(user_id: str, auth_context: dict | None) -> Any:
            return await get_user(user_id, auth_context or {})

        return reject_sync_access, aload_via_async

    return None


def default_django_user_loader(user_id: str, auth_context: dict | None) -> Any:
    """Default user query for schemes with no backend instance on the route
    (e.g. Rust-side session auth), via the pre-compiled pk query.

    Returns None for a stale user_id (user deleted after the session/token
    was issued), mirroring the framework get_user/get_user_sync defaults.
    """
    return run_orm_blocking(load_user_by_pk_sync, get_user_model(), user_id)


async def default_django_user_aloader(user_id: str, auth_context: dict | None) -> Any:
    """Async form of :func:`default_django_user_loader`, for ``await request.auser()``."""
    return await run_in_orm_executor(load_user_by_pk_sync, get_user_model(), user_id)


DEFAULT_USER_LOADERS: UserLoaders = (default_django_user_loader, default_django_user_aloader)


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

    loaders = _resolved_loader_registry.get(backend_name) if backend_name else None
    if loaders is None:
        return None
    return await loaders[1](user_id, auth_context)


def load_user_sync(
    user_id: str | None,
    backend_name: str | None,
    auth_context: dict | None = None,
) -> Any | None:
    """
    Synchronously load user from auth context.

    This is the sync version used by SimpleLazyObject for lazy loading.
    The query runs on the calling thread. With a running loop, it runs on
    the ORM pool.

    Args:
        user_id: User identifier from auth context
        backend_name: Authentication backend name (e.g., "jwt", "api_key")
        auth_context: Full authentication context dict

    Returns:
        User object, or None if not found or no user_id
    """
    if not user_id:
        return None

    if backend_name in _auth_backend_registry:
        loaders = _resolved_loader_registry[backend_name]
        if loaders is None:
            # Backend provides no user resolution (e.g. plain APIKeyAuthentication)
            return None
        return loaders[0](user_id, auth_context)

    # Unregistered backend (e.g. session auth): default Django user query
    return default_django_user_loader(user_id, auth_context)
