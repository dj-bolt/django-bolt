"""Logging middleware for Django-Bolt.

Provides request/response logging with support for Django's logging configuration.
"""

import logging
import random
from typing import Any

from .config import LoggingConfig, get_default_logging_config


class LoggingMiddleware:
    """Middleware for logging HTTP requests and responses.

    Integrates with Django's logging system and provides structured logging.
    """

    __slots__ = ("config", "logger", "_should_time_cached", "_request_debug_enabled_cached")

    def __init__(self, config: LoggingConfig | None = None):
        """Initialize logging middleware.

        Args:
            config: Logging configuration (uses defaults if not provided)
        """
        if config is None:
            config = get_default_logging_config()

        self.config = config
        self.logger = config.get_logger()

        # Pre-compute debug-level request logging gate at init.
        request_debug_enabled = False
        try:
            request_debug_enabled = self.logger.isEnabledFor(logging.DEBUG)
        except Exception:
            request_debug_enabled = False
        self._request_debug_enabled_cached = request_debug_enabled

        # Pre-compute timing decision at init (not per-request)
        should_time = False
        try:
            if self.logger.isEnabledFor(logging.INFO):
                should_time = True
        except Exception:
            should_time = False
        if not should_time:
            should_time = bool(getattr(config, "min_duration_ms", None))
        self._should_time_cached = should_time

    def obfuscate_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Obfuscate sensitive headers.

        Args:
            headers: Request/response headers

        Returns:
            Headers with sensitive values obfuscated
        """
        obfuscated = {}
        for key, value in headers.items():
            if key.lower() in self.config.obfuscate_headers:
                obfuscated[key] = "***"
            else:
                obfuscated[key] = value
        return obfuscated

    def obfuscate_cookies(self, cookies: dict[str, str]) -> dict[str, str]:
        """Obfuscate sensitive cookies.

        Args:
            cookies: Request cookies

        Returns:
            Cookies with sensitive values obfuscated
        """
        obfuscated = {}
        for key, value in cookies.items():
            if key in self.config.obfuscate_cookies:
                obfuscated[key] = "***"
            else:
                obfuscated[key] = value
        return obfuscated

    def extract_request_data(self, request: dict[str, Any]) -> dict[str, Any]:
        """Extract request data for logging.

        Args:
            request: Request dictionary

        Returns:
            Dictionary of request data to log
        """
        data = {}

        if "method" in self.config.request_log_fields:
            data["method"] = request.get("method", "")

        if "path" in self.config.request_log_fields:
            data["path"] = request.get("path", "")

        if "query" in self.config.request_log_fields:
            query_params = request.get("query_params", {})
            if query_params:
                data["query"] = query_params

        if "headers" in self.config.request_log_fields:
            headers = request.get("headers", {})
            if headers:
                data["headers"] = self.obfuscate_headers(headers)

        if "body" in self.config.request_log_fields and self.config.log_request_body:
            body = request.get("body", b"")
            if body and len(body) <= self.config.max_body_log_size:
                try:
                    data["body"] = body.decode("utf-8")
                except UnicodeDecodeError:
                    data["body"] = f"<binary data, {len(body)} bytes>"

        if "client_ip" in self.config.request_log_fields:
            # Runtime requests expose Bolt's canonical, proxy-aware address via
            # Django's existing REMOTE_ADDR. The header fallback is only for
            # standalone dict inputs used outside Bolt's request pipeline.
            headers = request.get("headers", {})
            meta = getattr(request, "META", None)
            client_ip = meta.get("REMOTE_ADDR", "") if meta is not None else ""
            if not client_ip and meta is None:
                client_ip = headers.get("x-forwarded-for", "").split(",")[0].strip() or headers.get("x-real-ip", "")
            if client_ip:
                data["client_ip"] = client_ip

        if "user_agent" in self.config.request_log_fields:
            headers = request.get("headers", {})
            user_agent = headers.get("user-agent", "")
            if user_agent:
                data["user_agent"] = user_agent

        return data

    def log_request(self, request: dict[str, Any]) -> None:
        """Log an HTTP request.

        Args:
            request: Request dictionary
        """
        path = request.get("path", "")
        if not self.config.should_log_request(path):
            return

        # Requests are always DEBUG level. Short-circuit if disabled.
        if not self.logger.isEnabledFor(logging.DEBUG):
            return

        data = self.extract_request_data(request)

        # Build log message from configured fields only
        message_parts = []
        if "method" in self.config.request_log_fields and "method" in data:
            message_parts.append(data["method"])
        if "path" in self.config.request_log_fields and "path" in data:
            message_parts.append(data["path"])

        message = " ".join(message_parts) if message_parts else f"Request: {path}"

        # Log requests at DEBUG level (less important than responses)
        self.logger.log(logging.DEBUG, message, extra=data)

    def log_response(
        self,
        request: dict[str, Any],
        status_code: int,
        duration: float,
        response_size: int | None = None,
    ) -> None:
        """Log an HTTP response.

        Args:
            request: Request dictionary
            status_code: HTTP status code
            duration: Request duration in seconds
            response_size: Response size in bytes (optional)
        """
        path = request.get("path", "")
        if not self.config.should_log_request(path, status_code):
            return

        # Determine log level early and short-circuit if disabled for success path
        if status_code >= 500:
            log_level = logging.ERROR
        elif status_code >= 400:
            log_level = logging.WARNING
        else:
            log_level = logging.INFO

        # For successful responses, apply gating: level check, sampling, and slow-only
        if status_code < 400:
            if not self.logger.isEnabledFor(log_level):
                return
            # Sampling gate
            if self.config.sample_rate is not None:
                try:
                    if random.random() > float(self.config.sample_rate):
                        return
                except Exception as e:
                    # If sampling check fails, log the request anyway (fail-open)
                    logging.getLogger(__name__).warning(
                        "Failed to apply sampling rate for request logging. "
                        "Logging request without sampling. Error: %s",
                        e,
                    )
            # Slow-only gate
            if self.config.min_duration_ms is not None:
                duration_ms_check = duration * 1000.0
                if duration_ms_check < float(self.config.min_duration_ms):
                    return

        data = {}
        message_parts = []

        # Only include method if configured
        if "method" in self.config.request_log_fields:
            method = request.get("method", "")
            data["method"] = method
            message_parts.append(method)

        # Only include path if configured
        if "path" in self.config.request_log_fields:
            data["path"] = path
            message_parts.append(path)

        # Only include status_code if configured
        if "status_code" in self.config.response_log_fields:
            data["status_code"] = status_code
            message_parts.append(f"{status_code}")

        # Only include duration if configured
        if "duration" in self.config.response_log_fields:
            duration_ms = round(duration * 1000, 2)
            data["duration_ms"] = duration_ms
            message_parts.append(f"({duration_ms}ms)")

        if "size" in self.config.response_log_fields and response_size is not None:
            data["response_size"] = response_size

        # Build log message from configured fields only
        message = " ".join(message_parts) if message_parts else f"Response: {status_code}"

        self.logger.log(log_level, message, extra=data)

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
def create_logging_middleware(
    logger_name: str | None = None, log_level: str | None = None, **kwargs
) -> LoggingMiddleware:
    """Create a logging middleware with custom configuration.

    Args:
        logger_name: Logger name (defaults to 'django.server')
        log_level: Log level (defaults to DEBUG in DEBUG mode, INFO otherwise)
        **kwargs: Additional configuration options

    Returns:
        LoggingMiddleware instance
    """
    config = get_default_logging_config()

    if logger_name:
        config.logger_name = logger_name

    if log_level:
        config.log_level = log_level

    # Update config with additional kwargs
    for key, value in kwargs.items():
        if hasattr(config, key):
            setattr(config, key, value)

    return LoggingMiddleware(config)
