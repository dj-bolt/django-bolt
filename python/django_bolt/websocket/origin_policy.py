"""WebSocket Origin policy configuration.

Resolve settings and per-endpoint overrides at registration, not on handshakes.
Python validators are opt-in and run only before the opening handshake.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import queue
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from ipaddress import IPv6Address
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

from django.conf import settings
from django.utils.module_loading import import_string

NativeOriginPolicy = Literal["inherit", "allowlist", "any", "custom"]
MountOriginPolicy = Literal["inherit", "allowlist", "any", "custom", "delegate"]


class OriginContext(TypedDict):
    """Handshake request data. Client headers do not prove identity."""

    path: str
    query_string: str
    headers: dict[str, str]


OriginValidator = Callable[[str | None, OriginContext], bool | Awaitable[bool]]

# Only custom policies pay for admission control. A dedicated pool prevents
# slow user callbacks from occupying bolt_default threads used by handlers.
_MAX_PENDING_CUSTOM_ORIGINS = 64
_SYNC_ORIGIN_WORKERS = 8
# Run Python's cancellation just ahead of the Rust five-second safety timeout.
# An uncooperative coroutine that suppresses cancellation retains its slot.
_PYTHON_ORIGIN_TIMEOUT_SECONDS = 4.9
_pending_custom_origins = threading.BoundedSemaphore(_MAX_PENDING_CUSTOM_ORIGINS)
_sync_origin_slots = threading.BoundedSemaphore(_SYNC_ORIGIN_WORKERS)
_sync_origin_executor_lock = threading.Lock()
# Python's ThreadPoolExecutor joins its non-daemon threads at interpreter
# exit. A user validator can block forever, so use a small daemon worker pool
# instead. The semaphore in _run_sync_origin_validator still limits admission;
# it releases each slot only when its Future ends (not on handshake timeout).
class _DaemonOriginExecutor:
    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue[tuple[concurrent.futures.Future, Callable, tuple]] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._outstanding = 0
        self._threads: list[threading.Thread] = []

    def submit(self, callback: Callable, *args: Any) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            if len(self._threads) < min(self._outstanding + 1, _SYNC_ORIGIN_WORKERS):
                worker = threading.Thread(
                    target=self._run, name=f"bolt_ws_origin_{len(self._threads)}", daemon=True
                )
                # A failed start must leave no queued job or phantom worker.
                worker.start()
                self._threads.append(worker)
            self._outstanding += 1
            self._jobs.put((future, callback, args))
        return future

    def _run(self) -> None:
        while True:
            future, callback, args = self._jobs.get()
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(callback(*args))
                    except BaseException as exc:
                        future.set_exception(exc)
            finally:
                with self._lock:
                    self._outstanding -= 1


_sync_origin_executor: _DaemonOriginExecutor | None = None


def _get_sync_origin_executor() -> _DaemonOriginExecutor:
    global _sync_origin_executor
    if _sync_origin_executor is None:
        with _sync_origin_executor_lock:
            if _sync_origin_executor is None:
                _sync_origin_executor = _DaemonOriginExecutor()
    return _sync_origin_executor


async def _run_sync_origin_validator(func: Callable, origin: str | None, context: OriginContext) -> Any:
    if not _sync_origin_slots.acquire(blocking=False):
        raise RuntimeError("Too many synchronous WebSocket Origin validations")
    try:
        future = _get_sync_origin_executor().submit(contextvars.copy_context().run, func, origin, context)
    except BaseException:
        _sync_origin_slots.release()
        raise
    # A running thread is not cancellable. The permit belongs to its concurrent
    # Future until it *actually* exits, even if the handshake times out first.
    future.add_done_callback(lambda _completed: _sync_origin_slots.release())
    return await asyncio.wrap_future(future)


@dataclass(frozen=True)
class OriginOptions:
    policy: MountOriginPolicy | None = None
    allowed_origins: list[str] | None = None
    allow_missing_origin: bool | None = None
    validator: OriginValidator | str | None = None


class ResolvedOrigin:
    """Registration wire format read exactly once by the Rust core."""

    def __init__(self, policy: str, origins: list[str], missing: bool, validator: OriginValidator | None):
        self._bolt_websocket_origin_policy = policy
        self._bolt_websocket_allowed_origins = origins
        self._bolt_websocket_allow_missing_origin = missing
        self._bolt_websocket_origin_validator = validator


def _valid_origin(origin: str) -> bool:
    if not origin or not origin.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in origin):
        return False
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        if not host or parsed.netloc.endswith(":"):
            return False
        if ":" in host:
            IPv6Address(host)
            if "%" in host:
                return False
        elif any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-_" for c in host):
            return False
        return (
            parsed.scheme in ("http", "https")
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and parsed.query == ""
            and parsed.fragment == ""
            and parsed.port != 0
            and origin == f"{parsed.scheme}://{parsed.netloc}"
        )
    except ValueError:
        return False


def _settings_value(name: str, fallback: Any, enabled: bool) -> Any:
    if not enabled or not settings.configured:
        return fallback
    return getattr(settings, name, fallback)


def resolve_origin_options(
    options: OriginOptions | None = None,
    *,
    read_settings: bool = True,
    asgi: bool = False,
) -> ResolvedOrigin:
    """Explicit endpoint values > Django settings > backward-compatible defaults."""
    local = options or OriginOptions()
    global_policy = _settings_value("BOLT_WEBSOCKET_ORIGIN_POLICY", "inherit", read_settings)
    policy = local.policy if local.policy is not None else global_policy
    if local.policy is None and policy == "delegate":
        raise ValueError("delegate must be selected explicitly on an ASGI mount")
    valid = ("inherit", "allowlist", "any", "custom", "delegate") if asgi else ("inherit", "allowlist", "any", "custom")
    if policy not in valid:
        raise ValueError(f"WebSocket Origin policy must be one of {valid}, got {policy!r}")

    global_origins = _settings_value("BOLT_WEBSOCKET_ALLOWED_ORIGINS", None, read_settings)
    origins = local.allowed_origins if local.allowed_origins is not None else global_origins
    if policy == "allowlist":
        if not isinstance(origins, (list, tuple)) or not origins:
            raise ValueError("WebSocket allowed_origins must be a nonempty list for 'allowlist'")
        for origin in origins:
            if not isinstance(origin, str) or not _valid_origin(origin):
                raise ValueError(f"Invalid WebSocket origin: {origin!r}")
    elif local.allowed_origins is not None:
        raise ValueError("allowed_origins requires Origin policy='allowlist'")

    global_missing = _settings_value("BOLT_WEBSOCKET_ALLOW_MISSING_ORIGIN", None, read_settings)
    missing = local.allow_missing_origin if local.allow_missing_origin is not None else global_missing
    if missing is None:
        missing = policy in ("inherit", "delegate")
    if type(missing) is not bool:
        raise TypeError("allow_missing_origin must be a bool or None")

    global_validator = _settings_value("BOLT_WEBSOCKET_ORIGIN_VALIDATOR", None, read_settings)
    validator = local.validator if local.validator is not None else global_validator
    if policy == "custom":
        if isinstance(validator, str):
            validator = import_string(validator)
        if not callable(validator):
            raise ValueError("Origin policy='custom' requires a callable origin_validator")
        func = validator

        async def checked(
            origin: str | None,
            context: OriginContext,
            admitted_at: float | None = None,
        ) -> bool:
            if not _pending_custom_origins.acquire(blocking=False):
                raise RuntimeError("Too many pending WebSocket Origin validations")
            try:
                # Rust timing out its await does not cancel the Python task
                # created by pyo3-async-runtimes. Cancel it on the Python loop
                # so a cooperative async validator releases admission promptly.
                loop = asyncio.get_running_loop()
                # Rust supplies the admission time in the monotonic clock.
                # Direct test-client calls start their own deadline.
                deadline = (
                    admitted_at if admitted_at is not None else loop.time()
                ) + _PYTHON_ORIGIN_TIMEOUT_SECONDS
                if loop.time() >= deadline:
                    raise TimeoutError("WebSocket Origin validation deadline expired")
                async with asyncio.timeout_at(deadline):
                    if inspect.iscoroutinefunction(func):
                        result = await func(origin, context)
                    else:
                        result = await _run_sync_origin_validator(func, origin, context)
                        if inspect.isawaitable(result):
                            result = await result
                    if type(result) is not bool:
                        raise TypeError("WebSocket origin_validator must return bool")
                    return result
            finally:
                _pending_custom_origins.release()

        validator = checked
    elif local.validator is not None:
        raise ValueError("origin_validator requires Origin policy='custom'")
    else:
        validator = None

    return ResolvedOrigin(policy, list(origins or ()) if policy == "allowlist" else [], missing, validator)
