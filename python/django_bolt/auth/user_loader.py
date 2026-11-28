"""
User loading system for request.user

Bolt loads the user lazily. LazyUser runs the query at the first read only.

Backends customize user loading by overriding get_user (async) and/or
get_user_sync (sync) — overriding either one alone is honored. The effective
strategy is resolved once per route at registration time (resolve_user_loader,
stored in the handler meta by backend position), so the per-request path is a
single tuple index plus a call. Rust reports that position as
auth_backend_index. Resolution must be per backend, not per scheme name: two
JWTAuthentication subclasses share scheme_name "jwt" but can carry different
get_user overrides.

Each backend resolves to a pair of loaders:

1. request.user is sync. It runs the query on the thread that reads it
   (run_orm_blocking), also on a thread with a running event loop: the loop
   waits in any case, and a hop would only add two thread wakeups. On the
   loop of a request with Django middleware, the query runs on the lane of
   the request, and the loop waits.
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

from django.utils.functional import SimpleLazyObject, empty

from ..concurrency import run_in_orm_executor, run_orm_blocking
from ..exceptions import Unauthorized
from .backends import BaseAuthentication, JWTAuthentication
from .pk_loader import load_user_by_pk_sync

__all__ = [
    "LazyUser",
    "aload_bolt_user",
    "load_user_by_pk_sync",
    "resolve_user_loader",
]

SyncLoader = Callable[[str, dict | None], Any]
AsyncLoader = Callable[[str, dict | None], Coroutine[Any, Any, Any]]
UserLoaders = tuple[SyncLoader, AsyncLoader]

# Framework-provided implementations. A backend method that is one of these
# was NOT overridden by the user — only genuine overrides take priority.
_FRAMEWORK_GET_USER = (BaseAuthentication.get_user, JWTAuthentication.get_user)
_FRAMEWORK_GET_USER_SYNC = (JWTAuthentication.get_user_sync,)


class LazyUser(SimpleLazyObject):
    """``request.user`` of a request that Bolt authenticated.

    Sync access forces the user on the calling thread, as ``SimpleLazyObject``
    does. ``await request.auser()`` calls :meth:`aload`, which awaits the
    async loader and does not block the event loop. Both share one result.

    The object is never None itself, because Bolt does not load the user
    before the first read. When the user ID has no row, the object is falsy,
    :meth:`aload` gives None, and the read of an attribute raises
    ``Unauthorized`` (401).
    """

    def __init__(self, loaders: UserLoaders, user_id: str, auth_context: dict | None) -> None:
        # Set the state of SimpleLazyObject.__init__ directly. Each request with a
        # user builds one, and the chain of __init__ calls costs more than the writes.
        # LazyObject.__setattr__ forwards other names to the wrapped user.
        state = self.__dict__
        state["_setupfunc"] = partial(loaders[0], user_id, auth_context)
        state["_wrapped"] = empty
        state["_aloader"] = loaders[1]

    def __getattr__(self, name: str) -> Any:
        # As LazyObject.__getattr__, with one more case: a user ID with no row
        # loads as None. The credential is valid, but its user does not exist.
        # Without this case, ``request.user.id`` raises AttributeError, a 500.
        if self._wrapped is empty:
            self._setup()
        user = self._wrapped
        if user is None:
            # Rust adds the challenge of the auth backends of the route to this 401.
            raise Unauthorized(detail="Authentication required")
        return getattr(user, name)

    def __repr__(self) -> str:
        # The arguments of the loader hold the auth context with the token claims.
        # A repr can reach a debug page or a log, so it shows the user ID only.
        if self._wrapped is empty:
            return f"<LazyUser: user_id={self._setupfunc.args[0]!r}, not loaded>"
        return f"<LazyUser: {self._wrapped!r}>"

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

        if custom_sync:

            async def aload_via_sync(user_id: str, auth_context: dict | None) -> Any:
                return await run_in_orm_executor(get_user_sync, user_id)

        else:
            # The loader of the framework. It runs ``get_user_sync`` on the ORM
            # pool, or its query on the event loop (BOLT_ASYNC_ORM_ON_LOOP).
            framework_get_user = backend.get_user

            async def aload_via_sync(user_id: str, auth_context: dict | None) -> Any:
                return await framework_get_user(user_id, auth_context or {})

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
