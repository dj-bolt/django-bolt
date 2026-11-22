"""Logging middleware for Django-Bolt.

Bolt calls ``log_exception()`` for a server error. The Rust server writes the
access log.
"""

from __future__ import annotations

import logging
from dataclasses import fields
from typing import Any

from .config import LoggingConfig, get_default_logging_config


class LoggingMiddleware:
    """The logger of an API, from its ``LoggingConfig``.

    Bolt calls ``log_exception()`` for a server error.
    """

    __slots__ = ("config", "logger")

    def __init__(self, config: LoggingConfig | None = None):
        """Initialize logging middleware.

        Args:
            config: Logging configuration (uses defaults if not provided)
        """
        if config is None:
            config = get_default_logging_config()

        self.config = config
        self.logger = config.get_logger()

    def log_exception(
        self,
        request: dict[str, Any],
        exc: Exception,
        exc_info: bool = True,
    ) -> None:
        """Log an exception that occurred during request handling.

        Args:
            request: Request dictionary
            exc: Exception instance
            exc_info: Whether to include exception traceback
        """
        path = request.get("path", "")

        data = {
            "method": request.get("method", ""),
            "path": path,
            "exception_type": type(exc).__name__,
            "exception": str(exc),
        }

        message = f"Exception in {data['method']} {path}: {type(exc).__name__}: {str(exc)}"

        # Use custom exception handler if provided
        if self.config.exception_logging_handler:
            self.config.exception_logging_handler(self.logger, request, exc, exc_info)
        else:
            # Default exception logging
            log_level = getattr(logging, self.config.error_log_level.upper(), logging.ERROR)
            self.logger.log(
                log_level,
                message,
                extra=data,
                exc_info=exc_info,
            )


# Convenience function to create logging middleware
def create_logging_middleware(logger_name: str | None = None, **kwargs) -> LoggingMiddleware:
    """Create a logging middleware with custom configuration.

    Args:
        logger_name: Logger name (defaults to 'django.server')
        **kwargs: Other options of ``LoggingConfig``. An unknown option raises ``TypeError``.

    Returns:
        LoggingMiddleware instance
    """
    options = {option.name for option in fields(LoggingConfig)}
    for key in kwargs:
        if key not in options:
            raise TypeError(f"create_logging_middleware() got an unknown option {key!r}")

    config = get_default_logging_config()

    if logger_name:
        config.logger_name = logger_name

    for key, value in kwargs.items():
        setattr(config, key, value)

    return LoggingMiddleware(config)
