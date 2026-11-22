"""Logging configuration for Django-Bolt.

Integrates with Django's logging configuration and provides structured logging
for HTTP requests, responses, and exceptions.

Adopts Litestar's queue-based logging approach so request logging stays
non-blocking and fully controlled by application logging config.
"""

from __future__ import annotations

import atexit
import contextlib
import importlib
import logging
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from logging.handlers import QueueHandler, QueueListener
from queue import Full, Queue

from django.core.exceptions import ImproperlyConfigured

try:
    from django.conf import settings
except ImportError:
    settings = None

# Global flag to prevent multiple logging reconfigurations
_LOGGING_CONFIGURED = False
_QUEUE_LISTENER: QueueListener | None = None
_QUEUE: Queue | None = None


@dataclass
class LoggingConfig:
    """Configuration for request/response logging.

    Integrates with Django's logging system and uses the configured logger.
    """

    # Logger name - defaults to Django's logger
    logger_name: str = "django.server"

    # Request logging fields
    request_log_fields: set[str] = field(default_factory=lambda: {"method", "path", "status_code"})

    # Response logging fields
    response_log_fields: set[str] = field(default_factory=lambda: {"status_code"})

    # Headers to obfuscate in logs (for security)
    obfuscate_headers: set[str] = field(
        default_factory=lambda: {"authorization", "cookie", "x-api-key", "x-auth-token"}
    )

    # Cookies to obfuscate in logs
    obfuscate_cookies: set[str] = field(default_factory=lambda: {"sessionid", "csrftoken"})

    # Log request body (be careful with sensitive data)
    log_request_body: bool = False

    # Log response body (be careful with large responses)
    log_response_body: bool = False

    # Maximum body size to log (in bytes)
    max_body_log_size: int = 1024

    # Note: Individual log levels are determined automatically:
    # - Requests: DEBUG
    # - Successful responses (2xx/3xx): INFO
    # - Client errors (4xx): WARNING
    # - Server errors (5xx): ERROR
    #
    # To control which logs appear, configure Django's LOGGING in settings.py:
    # LOGGING = {
    #     "loggers": {
    #         "django_bolt": {"level": "INFO"},  # Show INFO and above
    #     }
    # }

    # Deprecated: log_level is no longer used (kept for backward compatibility)
    log_level: str = "INFO"

    # Log level for exceptions (used by log_exception method)
    error_log_level: str = "ERROR"

    # Custom exception logging handler
    exception_logging_handler: Callable | None = None

    # The access log of runbolt writes no line for a request with one of these
    # paths (e.g., health checks). The match is exact, on the complete request path.
    skip_paths: set[str] = field(default_factory=lambda: {"/health", "/ready", "/metrics"})

    # The access log of runbolt writes no line for a response with one of these
    # status codes.
    skip_status_codes: set[int] = field(default_factory=set)

    # The access log of runbolt writes this part of the 1xx to 3xx responses
    # (0.0 to 1.0). None writes each of them. A 4xx or a 5xx is not sampled.
    sample_rate: float | None = None

    # The access log of runbolt writes a 1xx to 3xx response only when it took
    # this many milliseconds or more. A 4xx or a 5xx always writes its line.
    min_duration_ms: float | None = None

    def get_logger(self) -> logging.Logger:
        """Get the configured logger.

        Uses Django's logging configuration if available.
        """
        return logging.getLogger(self.logger_name)

    def should_log_request(self, path: str, status_code: int | None = None) -> bool:
        """Check if a request should be logged.

        Args:
            path: Request path
            status_code: Response status code (optional)

        Returns:
            True if request should be logged
        """
        if path in self.skip_paths:
            return False

        return not (status_code and status_code in self.skip_status_codes)


def _checked_sample_rate(value: object, name: str) -> float:
    """Return ``value`` as a sample rate, a number from 0.0 to 1.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a number from 0.0 to 1.0, got {value!r}")
    return float(value)


def _checked_min_duration_ms(value: object, name: str) -> float:
    """Return ``value`` as a duration in milliseconds, a number of 0 or more."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value >= 0:
        raise ValueError(f"{name} must be a number of 0 or more, got {value!r}")
    return float(value)


def _checked_skip_paths(value: object) -> tuple[str, ...]:
    """Return ``value`` as the sorted request paths that write no access-log line."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValueError(f"LoggingConfig.skip_paths must be a set of paths, got {value!r}")
    paths = tuple(value)
    for path in paths:
        if not isinstance(path, str):
            raise ValueError(f"LoggingConfig.skip_paths must hold only str paths, got {path!r}")
    return tuple(sorted(paths))


def _checked_skip_status_codes(value: object) -> tuple[int, ...]:
    """Return ``value`` as the sorted status codes that write no access-log line."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValueError(f"LoggingConfig.skip_status_codes must be a set of status codes, got {value!r}")
    codes = tuple(value)
    for code in codes:
        if isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599:
            raise ValueError(
                f"LoggingConfig.skip_status_codes must hold only status codes from 100 to 599, got {code!r}"
            )
    return tuple(sorted(codes))


def access_log_filter_meta(config: LoggingConfig) -> dict[str, float | tuple[str, ...] | tuple[int, ...]]:
    """Return the route metadata keys for the access-log filter of ``config``.

    Rust reads the keys one time, at startup. A key is absent when its option
    sets no limit. A value out of range raises ``ValueError``.
    """
    meta: dict[str, float | tuple[str, ...] | tuple[int, ...]] = {}
    skip_paths = _checked_skip_paths(config.skip_paths)
    if skip_paths:
        meta["access_log_skip_paths"] = skip_paths
    skip_status_codes = _checked_skip_status_codes(config.skip_status_codes)
    if skip_status_codes:
        meta["access_log_skip_status_codes"] = skip_status_codes
    if config.sample_rate is not None:
        sample_rate = _checked_sample_rate(config.sample_rate, "LoggingConfig.sample_rate")
        if sample_rate < 1.0:
            meta["access_log_sample_rate"] = sample_rate
    if config.min_duration_ms is not None:
        min_duration_ms = _checked_min_duration_ms(config.min_duration_ms, "LoggingConfig.min_duration_ms")
        if min_duration_ms > 0.0:
            meta["access_log_min_duration_ms"] = min_duration_ms
    return meta


def get_default_logging_config() -> LoggingConfig:
    """Get default logging configuration.

    The settings ``DJANGO_BOLT_LOG_SAMPLE`` and ``DJANGO_BOLT_LOG_SLOW_MS`` give
    ``sample_rate`` and ``min_duration_ms``. A value out of range raises
    ``ImproperlyConfigured``.
    """
    log_level = "INFO"
    settings_sample = None
    settings_slow_ms = None
    if settings is not None and settings.configured:
        settings_sample = getattr(settings, "DJANGO_BOLT_LOG_SAMPLE", None)
        settings_slow_ms = getattr(settings, "DJANGO_BOLT_LOG_SLOW_MS", None)
        log_level = "DEBUG" if settings.DEBUG else "WARNING"

    try:
        sample_rate = (
            None if settings_sample is None else _checked_sample_rate(settings_sample, "DJANGO_BOLT_LOG_SAMPLE")
        )
        min_duration_ms = (
            None if settings_slow_ms is None else _checked_min_duration_ms(settings_slow_ms, "DJANGO_BOLT_LOG_SLOW_MS")
        )
    except ValueError as error:
        raise ImproperlyConfigured(str(error)) from error

    return LoggingConfig(
        log_level=log_level,
        sample_rate=sample_rate,
        min_duration_ms=min_duration_ms,
    )


# Default bound for the logging queue (see BOLT_LOG_QUEUE_SIZE).
_DEFAULT_LOG_QUEUE_SIZE = 10000


class _DropOnFullQueueHandler(QueueHandler):
    """QueueHandler that drops records when the bounded queue is full.

    The stock ``QueueHandler`` calls ``Queue.put_nowait``, which raises
    ``queue.Full`` once the bounded queue saturates. ``emit`` then routes that
    to ``handleError``, which writes a traceback to stderr per record (because
    ``logging.raiseExceptions`` defaults to ``True``) — a stderr storm under the
    exact load spike the bound is meant to survive. Swallowing ``Full`` here
    drops the record while keeping the listener thread healthy.

    Drops are not fully silent: ``dropped_count`` tracks the total, and a single
    terse line is written straight to stderr on the first drop and then once per
    ``_DROP_WARN_INTERVAL`` drops. We write to stderr directly rather than via
    the logging system so the warning still surfaces when the queue (the very
    thing that is saturated) cannot accept more records, and so it can't recurse
    back into this handler.
    """

    # Surface saturation roughly this often without re-spamming under load.
    _DROP_WARN_INTERVAL = 10_000

    def __init__(self, queue: Queue) -> None:
        super().__init__(queue)
        self.dropped_count = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except Full:
            self.dropped_count += 1
            if self.dropped_count % self._DROP_WARN_INTERVAL == 1:
                sys.stderr.write(f"django-bolt: log queue saturated, dropped {self.dropped_count} record(s)\n")


class _BoundedQueueListener(QueueListener):
    """QueueListener that stops cleanly even when the bounded queue is full.

    The stock ``QueueListener.stop()`` calls ``enqueue_sentinel()``, which uses
    ``Queue.put_nowait`` — on a saturated bounded queue that raises ``queue.Full``
    and ``stop()`` fails, leaving the listener thread running. The listener thread
    is the consumer, so a *blocking* put is guaranteed to drain and succeed; use
    it for the sentinel so shutdown is reliable under load.
    """

    def enqueue_sentinel(self) -> None:
        self.queue.put(self._sentinel)


def _ensure_queue_logging(base_level: str) -> QueueHandler:
    """Create or reuse a queue-based logging setup.

    Returns a QueueHandler that enqueues log records. A singleton QueueListener
    forwards records to a console StreamHandler in the background. Inspired by
    Litestar's standard logging implementation.
    """

    global _QUEUE_LISTENER, _QUEUE  # noqa: PLW0603

    if _QUEUE is None:
        # Bounded queue prevents unbounded backlog → OOM under load spikes.
        # Records are dropped (via _DropOnFullQueueHandler) when full. The bound
        # is configurable via the BOLT_LOG_QUEUE_SIZE setting (default 10000),
        # consistent with the other BOLT_* logging settings.
        max_queue_size = _DEFAULT_LOG_QUEUE_SIZE
        if settings is not None:
            try:
                max_queue_size = int(getattr(settings, "BOLT_LOG_QUEUE_SIZE", _DEFAULT_LOG_QUEUE_SIZE))
            except Exception as exc:  # noqa: BLE001 - unconfigured settings or a bad value
                sys.stderr.write(f"django-bolt: invalid BOLT_LOG_QUEUE_SIZE ({exc}); using {_DEFAULT_LOG_QUEUE_SIZE}\n")
                max_queue_size = _DEFAULT_LOG_QUEUE_SIZE
            if max_queue_size <= 0:
                # Queue(maxsize<=0) is UNBOUNDED — exactly the OOM risk the bound
                # exists to prevent. Refuse it loudly and fall back to the default.
                sys.stderr.write(
                    f"django-bolt: BOLT_LOG_QUEUE_SIZE must be > 0 (got {max_queue_size}); "
                    f"using {_DEFAULT_LOG_QUEUE_SIZE}\n"
                )
                max_queue_size = _DEFAULT_LOG_QUEUE_SIZE
        _QUEUE = Queue(maxsize=max_queue_size)

    queue_handler = _DropOnFullQueueHandler(_QUEUE)
    queue_handler.setLevel(logging.DEBUG)

    if _QUEUE_LISTENER is None:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(base_level)
        console_handler.setFormatter(
            logging.Formatter(
                fmt="%(levelname)s - %(asctime)s - %(name)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

        listener = _BoundedQueueListener(_QUEUE, console_handler)
        listener.start()

        # Only register atexit once for cleanup
        def _cleanup_listener():
            """Safely stop the listener, handling already-stopped case."""
            try:
                if (
                    _QUEUE_LISTENER is not None
                    and hasattr(_QUEUE_LISTENER, "_thread")
                    and _QUEUE_LISTENER._thread is not None
                ):
                    _QUEUE_LISTENER.stop()
            except Exception as e:
                # Listener may already be stopped or in an invalid state
                # This can happen during normal shutdown, so log as debug
                logging.getLogger(__name__).debug(
                    "Failed to stop queue listener during cleanup. Listener may already be stopped. Error: %s", e
                )

        atexit.register(_cleanup_listener)
        _QUEUE_LISTENER = listener

    return queue_handler


def _hold_listener_handlers() -> None:
    """Take the lock of each listener handler before a fork.

    A fork does not reset the lock of a stream. If the fork occurs during a
    write of the listener, the child gets the lock of the stream in the held
    state. Then the listener of the child waits for the lock with no end.
    The handler lock is held during each write, so the fork waits for the write.
    """
    if _QUEUE_LISTENER is not None:
        for handler in _QUEUE_LISTENER.handlers:
            handler.acquire()


def _release_listener_handlers() -> None:
    """Release the handler locks in the parent after a fork.

    The ``logging`` module makes new handler locks in the child.
    """
    if _QUEUE_LISTENER is not None:
        for handler in reversed(_QUEUE_LISTENER.handlers):
            handler.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_hold_listener_handlers, after_in_parent=_release_listener_handlers)


def restart_queue_listener_after_fork() -> None:
    """Start a new queue listener in a forked child process.

    A fork copies the queue, but not the listener thread. Without a new
    listener, the records of the child stay in the queue, and nothing writes
    them. Call this function in the child, immediately after the fork.
    The function does nothing when no listener runs.
    """
    global _QUEUE_LISTENER  # noqa: PLW0603

    if _QUEUE_LISTENER is None or _QUEUE is None:
        return
    # Initialize the queue again. This clears the copied records, which the
    # listener of the parent writes. It also makes new locks, because a thread
    # of the parent can hold a lock at the time of the fork.
    _QUEUE.__init__(_QUEUE.maxsize)
    listener = _BoundedQueueListener(
        _QUEUE,
        *_QUEUE_LISTENER.handlers,
        respect_handler_level=_QUEUE_LISTENER.respect_handler_level,
    )
    listener.start()
    _QUEUE_LISTENER = listener


def stop_queue_listener() -> None:
    """Write the records in the queue, then stop the queue listener.

    ``os._exit()`` does not run the ``atexit`` functions. Call this function
    before ``os._exit()``, or the records in the queue are lost.
    The function does nothing when no listener runs.
    """
    global _QUEUE_LISTENER  # noqa: PLW0603

    if _QUEUE_LISTENER is None:
        return
    _QUEUE_LISTENER.stop()
    _QUEUE_LISTENER = None


def setup_django_logging(force: bool = False) -> None:
    """Setup Django logging configuration with console output.

    Configures Django's logging system to output to console/terminal.
    Based on Litestar's logging configuration pattern.

    This should be called once during application startup. Subsequent calls
    are no-ops unless force=True.

    Args:
        force: If True, reconfigure even if already configured
    """
    global _LOGGING_CONFIGURED, _QUEUE_LISTENER, _QUEUE

    # Guard against multiple reconfigurations (Litestar pattern)
    if _LOGGING_CONFIGURED and not force:
        return

    if force and _QUEUE_LISTENER is not None:
        with contextlib.suppress(Exception):
            _QUEUE_LISTENER.stop()
        _QUEUE_LISTENER = None

    try:
        # Check if Django is configured
        if settings is None or not settings.configured:
            return

        # Check if LOGGING is explicitly configured in Django settings
        # Note: Django's default settings may have LOGGING, but we want to check
        # if the user explicitly set it in their settings.py
        has_explicit_logging = False
        try:
            # Try to import the actual settings module to check if LOGGING is defined
            settings_module = importlib.import_module(settings.SETTINGS_MODULE)
            has_explicit_logging = hasattr(settings_module, "LOGGING")
        except (AttributeError, ImportError):
            # Fall back to checking settings object
            has_explicit_logging = hasattr(settings, "LOGGING") and settings.LOGGING

        if has_explicit_logging:
            # User has explicitly configured logging, respect it
            _LOGGING_CONFIGURED = True
            return

        # Get appropriate handlers for Python version
        base_level = "DEBUG" if getattr(settings, "DEBUG", False) else "WARNING"

        queue_handler = _ensure_queue_logging(base_level)

        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.addHandler(queue_handler)
        root_logger.setLevel(base_level)

        for logger_name in ("django", "django.server", "django_bolt"):
            logger = logging.getLogger(logger_name)
            logger.handlers.clear()
            logger.addHandler(queue_handler)
            # App-level logging config remains source of truth; use base_level if unset
            current_level = logger.level or logging.getLevelName(base_level)
            logger.setLevel(current_level)
            # Each of these loggers has the queue handler. A record that also
            # went to the root logger would print two times.
            logger.propagate = False

        _LOGGING_CONFIGURED = True

    except (ImportError, AttributeError, Exception):
        # If Django not available or configuration fails, use basic config
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s - %(asctime)s - %(name)s - %(message)s",
        )
        _LOGGING_CONFIGURED = True
