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
import logging.config
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from logging.handlers import QueueHandler, QueueListener
from queue import Full, Queue

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

    # Skip logging for specific paths (e.g., health checks)
    skip_paths: set[str] = field(default_factory=lambda: {"/health", "/ready", "/metrics"})

    # Skip logging for specific status codes
    skip_status_codes: set[int] = field(default_factory=set)

    # Optional sampling of logs (0.0-1.0). When set, successful responses (2xx/3xx)
    # will only be logged with this probability. Errors (4xx/5xx) are not sampled.
    sample_rate: float | None = None

    # Only log successful responses slower than this threshold (milliseconds).
    # Errors (4xx/5xx) are not subject to the slow-only threshold.
    min_duration_ms: int | None = None

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


@dataclass
class RequestLogFields:
    """Available fields for request logging."""

    # HTTP method (GET, POST, etc.)
    method: str = "method"

    # Request path
    path: str = "path"

    # Query string
    query: str = "query"

    # Request headers
    headers: str = "headers"

    # Request body
    body: str = "body"

    # Client IP address
    client_ip: str = "client_ip"

    # User agent
    user_agent: str = "user_agent"

    # Request ID (if available)
    request_id: str = "request_id"


@dataclass
class ResponseLogFields:
    """Available fields for response logging."""

    # HTTP status code
    status_code: str = "status_code"

    # Response headers
    headers: str = "headers"

    # Response body
    body: str = "body"

    # Response time (in seconds)
    duration: str = "duration"

    # Response size (in bytes)
    size: str = "size"


def get_default_logging_config() -> LoggingConfig:
    """Get default logging configuration.

    Uses Django's DEBUG setting to determine log level.
    """
    log_level = "INFO"
    debug = False
    settings_level = None
    settings_sample = None
    settings_slow_ms = None
    try:
        if settings is not None and settings.configured:
            debug = settings.DEBUG
            # Optional overrides from Django settings
            settings_level = getattr(settings, "DJANGO_BOLT_LOG_LEVEL", None)
            settings_sample = getattr(settings, "DJANGO_BOLT_LOG_SAMPLE", None)
            settings_slow_ms = getattr(settings, "DJANGO_BOLT_LOG_SLOW_MS", None)
            # Default base level by DEBUG
            log_level = "DEBUG" if debug else "WARNING"
    except (AttributeError, Exception) as e:
        # Django not available or not configured, use default
        logging.getLogger(__name__).debug(
            "Failed to read Django settings for logging configuration. Using default log level. Error: %s", e
        )

    # Choose log level: Django settings override > default determined by DEBUG
    if settings_level:
        log_level = str(settings_level).upper()

    sample_rate: float | None = None
    if settings_sample is not None:
        try:
            sr = float(settings_sample)
            if 0.0 <= sr <= 1.0:
                sample_rate = sr
        except Exception:
            sample_rate = None

    min_duration_ms: int | None = None
    if settings_slow_ms is not None:
        try:
            min_duration_ms = max(0, int(settings_slow_ms))
        except Exception:
            min_duration_ms = None
    else:
        # Default to slow-only logging in production
        if not debug:
            min_duration_ms = 250

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
            logger.propagate = logger_name == "django"

        _LOGGING_CONFIGURED = True

    except (ImportError, AttributeError, Exception):
        # If Django not available or configuration fails, use basic config
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s - %(asctime)s - %(name)s - %(message)s",
        )
        _LOGGING_CONFIGURED = True
