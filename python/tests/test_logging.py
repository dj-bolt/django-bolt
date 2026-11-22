"""Comprehensive tests for Django-Bolt logging system.

These tests validate the behavior documented in docs/LOGGING.md:
- Queue-based non-blocking logging
- Production defaults (min_duration_ms, sample_rate)
- Request/response logging with proper log levels
- Skip paths and status codes
- Header/cookie obfuscation
- Integration with Django's logging system
"""

import _thread
import io
import logging
import os
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import redirect_stderr
from logging.handlers import QueueHandler
from queue import Queue
from unittest.mock import Mock

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

import django_bolt.logging.config as config_module
from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig, LoggingMiddleware, create_logging_middleware
from django_bolt.logging.config import (
    _BoundedQueueListener,
    _DropOnFullQueueHandler,
    _ensure_queue_logging,
    get_default_logging_config,
    restart_queue_listener_after_fork,
    setup_django_logging,
    stop_queue_listener,
)


class TestLoggingConfig:
    """Test LoggingConfig behavior documented in LOGGING.md."""

    def test_default_config_uses_django_server_logger(self):
        """Default logger should be 'django.server' as documented."""
        config = LoggingConfig()
        assert config.logger_name == "django.server", "Default logger must be 'django.server' per LOGGING.md"

    def test_default_request_fields_include_basic_info(self):
        """Default request fields should include method, path, and status_code."""
        config = LoggingConfig()
        assert "method" in config.request_log_fields
        assert "path" in config.request_log_fields
        assert "status_code" in config.request_log_fields

    def test_default_response_fields_include_status_code(self):
        """Default response fields should include status_code."""
        config = LoggingConfig()
        assert "status_code" in config.response_log_fields

    def test_default_security_headers_are_obfuscated(self):
        """Security-sensitive headers should be obfuscated by default."""
        config = LoggingConfig()
        assert "authorization" in config.obfuscate_headers
        assert "cookie" in config.obfuscate_headers
        assert "x-api-key" in config.obfuscate_headers
        assert "x-auth-token" in config.obfuscate_headers

    def test_default_security_cookies_are_obfuscated(self):
        """Security-sensitive cookies should be obfuscated by default."""
        config = LoggingConfig()
        assert "sessionid" in config.obfuscate_cookies
        assert "csrftoken" in config.obfuscate_cookies

    def test_default_skip_paths_include_health_checks(self):
        """Health check paths should be skipped by default."""
        config = LoggingConfig()
        assert "/health" in config.skip_paths
        assert "/ready" in config.skip_paths
        assert "/metrics" in config.skip_paths

    def test_custom_logger_name(self):
        """Custom logger name should be configurable."""
        config = LoggingConfig(logger_name="custom.logger")
        assert config.logger_name == "custom.logger"

    def test_custom_request_fields(self):
        """Custom request fields should be configurable."""
        config = LoggingConfig(request_log_fields={"method", "path", "body", "client_ip"})
        assert "body" in config.request_log_fields
        assert "client_ip" in config.request_log_fields

    def test_custom_response_fields(self):
        """Custom response fields should be configurable."""
        config = LoggingConfig(response_log_fields={"status_code", "duration", "size"})
        assert "status_code" in config.response_log_fields
        assert "duration" in config.response_log_fields
        assert "size" in config.response_log_fields

    def test_sample_rate_configuration(self):
        """Sample rate should be configurable for reducing 2xx/3xx log volume."""
        config = LoggingConfig(sample_rate=0.05)
        assert config.sample_rate == 0.05

    def test_min_duration_ms_configuration(self):
        """Minimum duration threshold should be configurable for slow-only logging."""
        config = LoggingConfig(min_duration_ms=500)
        assert config.min_duration_ms == 500

    @pytest.mark.parametrize(
        ("name", "value", "message"),
        [
            ("DJANGO_BOLT_LOG_SAMPLE", 5, "DJANGO_BOLT_LOG_SAMPLE must be a number from 0.0 to 1.0, got 5"),
            ("DJANGO_BOLT_LOG_SAMPLE", "0.5", "DJANGO_BOLT_LOG_SAMPLE must be a number from 0.0 to 1.0, got '0.5'"),
            ("DJANGO_BOLT_LOG_SLOW_MS", -1, "DJANGO_BOLT_LOG_SLOW_MS must be a number of 0 or more, got -1"),
            ("DJANGO_BOLT_LOG_SLOW_MS", True, "DJANGO_BOLT_LOG_SLOW_MS must be a number of 0 or more, got True"),
        ],
    )
    def test_a_log_setting_out_of_range_stops_the_api(self, settings, name, value, message):
        """A wrong value of a log setting is an error, not a setting with no effect."""
        setattr(settings, name, value)
        with pytest.raises(ImproperlyConfigured, match=re.escape(message)):
            BoltAPI()

    @pytest.mark.parametrize(
        ("config", "message"),
        [
            (LoggingConfig(sample_rate=1.5), "LoggingConfig.sample_rate must be a number from 0.0 to 1.0, got 1.5"),
            (
                LoggingConfig(min_duration_ms=-250),
                "LoggingConfig.min_duration_ms must be a number of 0 or more, got -250",
            ),
        ],
    )
    def test_a_logging_config_value_out_of_range_stops_the_api(self, config, message):
        with pytest.raises(ValueError, match=re.escape(message)):
            BoltAPI(logging_config=config)

    @pytest.mark.parametrize(
        ("config", "message"),
        [
            (LoggingConfig(skip_paths="/health"), "LoggingConfig.skip_paths must be a set of paths, got '/health'"),
            (
                LoggingConfig(skip_paths={b"/health"}),
                "LoggingConfig.skip_paths must hold only str paths, got b'/health'",
            ),
            (
                LoggingConfig(skip_status_codes=204),
                "LoggingConfig.skip_status_codes must be a set of status codes, got 204",
            ),
            (
                LoggingConfig(skip_status_codes={"204"}),
                "LoggingConfig.skip_status_codes must hold only status codes from 100 to 599, got '204'",
            ),
            (
                LoggingConfig(skip_status_codes={600}),
                "LoggingConfig.skip_status_codes must hold only status codes from 100 to 599, got 600",
            ),
        ],
    )
    def test_a_skip_option_of_a_wrong_type_stops_the_api(self, config, message):
        """Rust applies the two skip options, so a value that it cannot read is an error."""
        with pytest.raises(ValueError, match=re.escape(message)):
            BoltAPI(logging_config=config)

    def test_the_default_config_takes_the_two_log_settings(self, settings):
        """Without the settings, the default config sets no limit, also with DEBUG off."""
        settings.DEBUG = False
        default = get_default_logging_config()
        assert (default.sample_rate, default.min_duration_ms) == (None, None)

        settings.DJANGO_BOLT_LOG_SAMPLE = 0.25
        settings.DJANGO_BOLT_LOG_SLOW_MS = 300
        from_settings = get_default_logging_config()
        assert (from_settings.sample_rate, from_settings.min_duration_ms) == (0.25, 300.0)

    def test_skip_paths_configuration(self):
        """Skip paths should be fully customizable."""
        config = LoggingConfig(skip_paths={"/admin", "/static"})
        assert "/admin" in config.skip_paths
        assert "/static" in config.skip_paths

    def test_skip_status_codes_configuration(self):
        """Skip status codes should be configurable."""
        config = LoggingConfig(skip_status_codes={204, 304})
        assert 204 in config.skip_status_codes
        assert 304 in config.skip_status_codes

    def test_should_log_request_returns_true_for_normal_paths(self):
        """Normal paths should be logged."""
        config = LoggingConfig(skip_paths={"/health"})
        assert config.should_log_request("/api/users") is True

    def test_should_log_request_returns_false_for_skipped_paths(self):
        """Paths in skip_paths should not be logged."""
        config = LoggingConfig(skip_paths={"/health", "/metrics"})
        assert config.should_log_request("/health") is False
        assert config.should_log_request("/metrics") is False

    def test_should_log_request_returns_false_for_skipped_status_codes(self):
        """Status codes in skip_status_codes should not be logged."""
        config = LoggingConfig(skip_status_codes={204, 304})
        assert config.should_log_request("/api/users", 204) is False
        assert config.should_log_request("/api/users", 304) is False

    def test_should_log_request_returns_true_for_non_skipped_status_codes(self):
        """Non-skipped status codes should be logged."""
        config = LoggingConfig(skip_status_codes={204, 304})
        assert config.should_log_request("/api/users", 200) is True
        assert config.should_log_request("/api/users", 500) is True

    def test_get_logger_returns_logger_instance(self):
        """get_logger() should return a proper Logger instance."""
        config = LoggingConfig(logger_name="test.logger")
        logger = config.get_logger()
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test.logger"


class TestLoggingMiddleware:
    """Test LoggingMiddleware behavior."""

    def test_middleware_uses_default_config_when_none_provided(self):
        """Middleware should use default config when none is provided."""
        middleware = LoggingMiddleware()
        assert middleware.config is not None
        assert middleware.logger is not None

    def test_middleware_uses_provided_config(self):
        """Middleware should use provided config."""
        config = LoggingConfig(logger_name="custom.logger")
        middleware = LoggingMiddleware(config)
        assert middleware.config.logger_name == "custom.logger"

    def test_obfuscate_headers_masks_sensitive_headers(self):
        """Sensitive headers should be obfuscated to '***'."""
        config = LoggingConfig(obfuscate_headers={"authorization", "x-api-key"})
        middleware = LoggingMiddleware(config)

        headers = {
            "content-type": "application/json",
            "authorization": "Bearer secret-token-12345",
            "x-api-key": "my-secret-key",
            "user-agent": "test-agent",
        }

        obfuscated = middleware.obfuscate_headers(headers)
        assert obfuscated["content-type"] == "application/json"
        assert obfuscated["authorization"] == "***", "Authorization header must be obfuscated"
        assert obfuscated["x-api-key"] == "***", "X-API-Key header must be obfuscated"
        assert obfuscated["user-agent"] == "test-agent"

    def test_obfuscate_headers_is_case_insensitive(self):
        """Header obfuscation should be case-insensitive."""
        config = LoggingConfig(obfuscate_headers={"authorization"})
        middleware = LoggingMiddleware(config)

        headers = {
            "Authorization": "Bearer token",
            "AUTHORIZATION": "Bearer token2",
        }

        obfuscated = middleware.obfuscate_headers(headers)
        assert obfuscated["Authorization"] == "***"
        assert obfuscated["AUTHORIZATION"] == "***"

    def test_obfuscate_cookies_masks_sensitive_cookies(self):
        """Sensitive cookies should be obfuscated to '***'."""
        config = LoggingConfig(obfuscate_cookies={"sessionid", "csrftoken"})
        middleware = LoggingMiddleware(config)

        cookies = {
            "sessionid": "abc123xyz789",
            "csrftoken": "token456def",
            "preferences": "dark-mode",
        }

        obfuscated = middleware.obfuscate_cookies(cookies)
        assert obfuscated["sessionid"] == "***", "sessionid cookie must be obfuscated"
        assert obfuscated["csrftoken"] == "***", "csrftoken cookie must be obfuscated"
        assert obfuscated["preferences"] == "dark-mode"

    def test_extract_request_data_includes_configured_fields_only(self):
        """Only configured request fields should be extracted."""
        config = LoggingConfig(request_log_fields={"method", "path"})
        middleware = LoggingMiddleware(config)

        request = {
            "method": "POST",
            "path": "/api/users",
            "query_params": {"page": "1"},
            "headers": {"content-type": "application/json"},
        }

        data = middleware.extract_request_data(request)
        assert "method" in data
        assert "path" in data
        assert "query" not in data, "Query should not be included when not configured"
        assert "headers" not in data, "Headers should not be included when not configured"

    def test_extract_request_data_includes_query_params_when_configured(self):
        """Query params should be included when configured."""
        config = LoggingConfig(request_log_fields={"method", "path", "query"})
        middleware = LoggingMiddleware(config)

        request = {
            "method": "GET",
            "path": "/api/users",
            "query_params": {"page": "1", "limit": "10"},
        }

        data = middleware.extract_request_data(request)
        assert "query" in data
        assert data["query"] == {"page": "1", "limit": "10"}

    def test_extract_request_data_obfuscates_headers(self):
        """Headers should be obfuscated when included."""
        config = LoggingConfig(
            request_log_fields={"method", "path", "headers"},
            obfuscate_headers={"authorization"},
        )
        middleware = LoggingMiddleware(config)

        request = {
            "method": "POST",
            "path": "/api/users",
            "headers": {
                "content-type": "application/json",
                "authorization": "Bearer secret-token",
            },
        }

        data = middleware.extract_request_data(request)
        assert "headers" in data
        assert data["headers"]["authorization"] == "***", "Authorization must be obfuscated"
        assert data["headers"]["content-type"] == "application/json"

    def test_extract_request_data_includes_body_when_configured(self):
        """Request body should be included when log_request_body=True."""
        config = LoggingConfig(
            request_log_fields={"method", "path", "body"},
            log_request_body=True,
            max_body_log_size=100,
        )
        middleware = LoggingMiddleware(config)

        request = {
            "method": "POST",
            "path": "/api/users",
            "body": b'{"name": "test", "email": "test@example.com"}',
        }

        data = middleware.extract_request_data(request)
        assert "body" in data
        assert "test@example.com" in data["body"]

    def test_extract_request_data_skips_body_when_too_large(self):
        """Request body should be skipped when exceeding max_body_log_size."""
        config = LoggingConfig(
            request_log_fields={"method", "path", "body"},
            log_request_body=True,
            max_body_log_size=10,
        )
        middleware = LoggingMiddleware(config)

        request = {
            "method": "POST",
            "path": "/api/users",
            "body": b'{"name": "test", "email": "test@example.com"}',  # >10 bytes
        }

        data = middleware.extract_request_data(request)
        assert "body" not in data, "Body should not be included when too large"

    def test_extract_request_data_handles_binary_body(self):
        """Binary body should be logged with size info."""
        config = LoggingConfig(
            request_log_fields={"method", "path", "body"},
            log_request_body=True,
            max_body_log_size=100,
        )
        middleware = LoggingMiddleware(config)

        request = {
            "method": "POST",
            "path": "/api/upload",
            "body": b"\xff\xfe\x00\x01",  # Binary data
        }

        data = middleware.extract_request_data(request)
        assert "body" in data
        assert "<binary data," in data["body"]
        assert "bytes>" in data["body"]

    def test_extract_request_data_extracts_client_ip_from_x_forwarded_for(self):
        """Client IP should be extracted from X-Forwarded-For header."""
        config = LoggingConfig(request_log_fields={"client_ip"})
        middleware = LoggingMiddleware(config)

        request = {
            "method": "GET",
            "path": "/api/users",
            "headers": {"x-forwarded-for": "192.168.1.1, 10.0.0.1"},
        }

        data = middleware.extract_request_data(request)
        assert data["client_ip"] == "192.168.1.1", "Should extract first IP from X-Forwarded-For"

    def test_extract_request_data_extracts_client_ip_from_x_real_ip(self):
        """Client IP should be extracted from X-Real-IP header."""
        config = LoggingConfig(request_log_fields={"client_ip"})
        middleware = LoggingMiddleware(config)

        request = {
            "method": "GET",
            "path": "/api/users",
            "headers": {"x-real-ip": "192.168.1.2"},
        }

        data = middleware.extract_request_data(request)
        assert data["client_ip"] == "192.168.1.2"

    def test_extract_request_data_prefers_canonical_client_ip(self):
        """Runtime REMOTE_ADDR must win over client-controlled headers."""
        config = LoggingConfig(request_log_fields={"client_ip"})
        middleware = LoggingMiddleware(config)
        request_data = {"headers": {"x-forwarded-for": "1.1.1.1"}}
        request = Mock()
        request.get.side_effect = request_data.get
        request.META = {"REMOTE_ADDR": "203.0.113.9"}

        assert middleware.extract_request_data(request)["client_ip"] == "203.0.113.9"

    def test_extract_request_data_extracts_user_agent(self):
        """User agent should be extracted when configured."""
        config = LoggingConfig(request_log_fields={"user_agent"})
        middleware = LoggingMiddleware(config)

        request = {
            "method": "GET",
            "path": "/api/users",
            "headers": {"user-agent": "Mozilla/5.0"},
        }

        data = middleware.extract_request_data(request)
        assert data["user_agent"] == "Mozilla/5.0"

    def test_log_request_skips_when_path_in_skip_paths(self, caplog):
        """Requests to skip_paths should not be logged."""
        config = LoggingConfig(skip_paths={"/health"})
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/health"}

        with caplog.at_level(logging.DEBUG):
            middleware.log_request(request)

        assert len(caplog.records) == 0, "Health checks should not be logged"

    def test_log_request_logs_when_debug_enabled(self, caplog):
        """Requests should be logged at DEBUG level when logger is enabled for DEBUG."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/users"}

        with caplog.at_level(logging.DEBUG, logger="test.logger"):
            middleware.log_request(request)

        # Should have logged at DEBUG level
        assert len(caplog.records) > 0, "Request should be logged at DEBUG level"
        assert caplog.records[0].levelno == logging.DEBUG

    def test_log_request_skips_when_debug_disabled(self, caplog):
        """Requests should not be logged when logger is not enabled for DEBUG."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/users"}

        # Set level to INFO (higher than DEBUG)
        with caplog.at_level(logging.INFO, logger="test.logger"):
            middleware.log_request(request)

        # Should not log because DEBUG is disabled
        assert len(caplog.records) == 0, "Request should not be logged when DEBUG is disabled"

    def test_log_response_uses_info_level_for_2xx(self, caplog):
        """Successful 2xx responses should be logged at INFO level."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "POST", "path": "/api/users"}

        with caplog.at_level(logging.INFO, logger="test.logger"):
            middleware.log_response(request, 201, 0.1)

        assert len(caplog.records) > 0, "2xx response should be logged"
        assert caplog.records[0].levelno == logging.INFO

    def test_log_response_uses_info_level_for_3xx(self, caplog):
        """Redirect 3xx responses should be logged at INFO level."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/redirect"}

        with caplog.at_level(logging.INFO, logger="test.logger"):
            middleware.log_response(request, 302, 0.05)

        assert len(caplog.records) > 0, "3xx response should be logged"
        assert caplog.records[0].levelno == logging.INFO

    def test_log_response_uses_warning_level_for_4xx(self, caplog):
        """Client error 4xx responses should be logged at WARNING level."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/notfound"}

        with caplog.at_level(logging.WARNING, logger="test.logger"):
            middleware.log_response(request, 404, 0.05)

        assert len(caplog.records) > 0, "4xx response should be logged"
        assert caplog.records[0].levelno == logging.WARNING

    def test_log_response_uses_error_level_for_5xx(self, caplog):
        """Server error 5xx responses should be logged at ERROR level."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/error"}

        with caplog.at_level(logging.ERROR, logger="test.logger"):
            middleware.log_response(request, 500, 0.1)

        assert len(caplog.records) > 0, "5xx response should be logged"
        assert caplog.records[0].levelno == logging.ERROR

    def test_log_response_skips_for_skip_paths(self, caplog):
        """Responses for skip_paths should not be logged."""
        config = LoggingConfig(skip_paths={"/metrics"})
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/metrics"}

        with caplog.at_level(logging.INFO):
            middleware.log_response(request, 200, 0.01)

        assert len(caplog.records) == 0, "Metrics endpoint should not be logged"

    def test_log_response_skips_for_skip_status_codes(self, caplog):
        """Responses with skip_status_codes should not be logged."""
        config = LoggingConfig(skip_status_codes={204})
        middleware = LoggingMiddleware(config)

        request = {"method": "DELETE", "path": "/api/users/1"}

        with caplog.at_level(logging.INFO):
            middleware.log_response(request, 204, 0.01)

        assert len(caplog.records) == 0, "204 responses should not be logged when configured"

    def test_log_response_skips_when_info_disabled_for_2xx(self, caplog):
        """2xx responses should not be logged when logger is not enabled for INFO."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/users"}

        # Set level to ERROR (higher than INFO)
        with caplog.at_level(logging.ERROR, logger="test.logger"):
            middleware.log_response(request, 200, 0.1)

        # Should not log 2xx when INFO is disabled
        assert len(caplog.records) == 0, "2xx should not be logged when INFO is disabled"

    def test_log_response_always_logs_4xx_regardless_of_level(self, caplog):
        """4xx errors should always be logged even if WARNING is disabled (they use WARNING level)."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/notfound"}

        # Set level to WARNING to capture WARNING logs
        with caplog.at_level(logging.WARNING, logger="test.logger"):
            middleware.log_response(request, 404, 0.05)

        # 4xx should be logged at WARNING level
        assert len(caplog.records) > 0, "4xx should be logged at WARNING level"

    def test_log_response_always_logs_5xx_regardless_of_level(self, caplog):
        """5xx errors should always be logged at ERROR level."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/error"}

        with caplog.at_level(logging.ERROR, logger="test.logger"):
            middleware.log_response(request, 500, 0.1)

        # 5xx should be logged at ERROR level
        assert len(caplog.records) > 0, "5xx should be logged at ERROR level"

    def test_log_response_respects_sample_rate_for_2xx(self):
        """Sample rate should reduce 2xx/3xx log volume probabilistically."""
        config = LoggingConfig(sample_rate=0.0, logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/users"}

        # With sample_rate=0.0, no 2xx responses should be logged
        logged_count = 0
        for _ in range(100):
            logger = logging.getLogger("test.logger")
            logger.handlers.clear()
            handler = logging.Handler()
            handler.handle = Mock()
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)

            middleware.log_response(request, 200, 0.1)

            if handler.handle.called:
                logged_count += 1

        assert logged_count == 0, "With sample_rate=0.0, no 2xx responses should be logged"

    def test_log_response_does_not_sample_errors(self, caplog):
        """Sample rate should NOT affect 4xx/5xx errors - they should always be logged."""
        config = LoggingConfig(sample_rate=0.0, logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/error"}

        # Even with sample_rate=0.0, errors should be logged
        with caplog.at_level(logging.ERROR, logger="test.logger"):
            middleware.log_response(request, 500, 0.1)

        assert len(caplog.records) > 0, "Errors should be logged regardless of sample_rate"

    def test_log_response_respects_min_duration_ms_for_2xx(self, caplog):
        """Fast 2xx responses should be skipped when below min_duration_ms threshold."""
        config = LoggingConfig(min_duration_ms=250, logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/users"}

        # Fast response (100ms < 250ms)
        with caplog.at_level(logging.INFO, logger="test.logger"):
            middleware.log_response(request, 200, 0.1)

        assert len(caplog.records) == 0, "Fast 2xx responses should not be logged with min_duration_ms"

    def test_log_response_logs_slow_2xx_above_min_duration_ms(self, caplog):
        """Slow 2xx responses should be logged when above min_duration_ms threshold."""
        config = LoggingConfig(min_duration_ms=250, logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/users"}

        # Slow response (300ms > 250ms)
        with caplog.at_level(logging.INFO, logger="test.logger"):
            middleware.log_response(request, 200, 0.3)

        assert len(caplog.records) > 0, "Slow 2xx responses should be logged"

    def test_log_response_ignores_min_duration_ms_for_errors(self, caplog):
        """min_duration_ms should NOT affect 4xx/5xx errors - they should always be logged."""
        config = LoggingConfig(min_duration_ms=250, logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/error"}

        # Fast error (50ms < 250ms)
        with caplog.at_level(logging.ERROR, logger="test.logger"):
            middleware.log_response(request, 500, 0.05)

        assert len(caplog.records) > 0, "Errors should be logged regardless of min_duration_ms"

    def test_log_response_includes_duration_in_milliseconds(self, caplog):
        """Duration should be logged in milliseconds when configured."""
        config = LoggingConfig(logger_name="test.logger", response_log_fields={"status_code", "duration"})
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/users"}

        with caplog.at_level(logging.INFO, logger="test.logger"):
            middleware.log_response(request, 200, 0.123)

        assert len(caplog.records) > 0
        log_message = caplog.records[0].message
        assert "123" in log_message, "Duration should be in milliseconds"
        assert "ms" in log_message

    def test_log_response_includes_response_size_when_configured(self, caplog):
        """Response size should be logged when configured."""
        config = LoggingConfig(logger_name="test.logger", response_log_fields={"status_code", "size"})
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/users"}

        with caplog.at_level(logging.INFO, logger="test.logger"):
            middleware.log_response(request, 200, 0.1, response_size=1024)

        assert len(caplog.records) > 0
        assert caplog.records[0].response_size == 1024

    def test_log_exception_logs_at_error_level(self, caplog):
        """Exceptions should be logged at ERROR level by default."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/error"}
        exc = ValueError("Something went wrong")

        with caplog.at_level(logging.ERROR, logger="test.logger"):
            middleware.log_exception(request, exc, exc_info=False)

        assert len(caplog.records) > 0, "Exception should be logged"
        assert caplog.records[0].levelno == logging.ERROR

    def test_log_exception_includes_exception_details(self, caplog):
        """Exception logs should include exception type and message."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "POST", "path": "/api/users"}
        exc = ValueError("Invalid user data")

        with caplog.at_level(logging.ERROR, logger="test.logger"):
            middleware.log_exception(request, exc, exc_info=False)

        assert len(caplog.records) > 0
        record = caplog.records[0]
        assert "ValueError" in record.message
        assert "Invalid user data" in record.message
        assert record.exception_type == "ValueError"
        assert record.exception == "Invalid user data"

    def test_log_exception_includes_request_context(self, caplog):
        """Exception logs should include request method and path."""
        config = LoggingConfig(logger_name="test.logger")
        middleware = LoggingMiddleware(config)

        request = {"method": "DELETE", "path": "/api/users/123"}
        exc = RuntimeError("Deletion failed")

        with caplog.at_level(logging.ERROR, logger="test.logger"):
            middleware.log_exception(request, exc, exc_info=False)

        assert len(caplog.records) > 0
        record = caplog.records[0]
        assert record.method == "DELETE"
        assert record.path == "/api/users/123"

    def test_log_exception_uses_custom_handler_when_provided(self):
        """Custom exception handler should be called when provided."""
        custom_handler = Mock()
        config = LoggingConfig(logger_name="test.logger", exception_logging_handler=custom_handler)
        middleware = LoggingMiddleware(config)

        request = {"method": "GET", "path": "/api/error"}
        exc = Exception("Test error")

        middleware.log_exception(request, exc, exc_info=True)

        custom_handler.assert_called_once()
        args = custom_handler.call_args[0]
        assert args[1] == request  # request
        assert args[2] == exc  # exception


class TestLoggingHelpers:
    """Test logging helper functions."""

    def test_create_logging_middleware_with_custom_logger_name(self):
        """create_logging_middleware should accept custom logger name."""
        middleware = create_logging_middleware(logger_name="custom.logger")
        assert isinstance(middleware, LoggingMiddleware)
        assert middleware.config.logger_name == "custom.logger"

    def test_create_logging_middleware_with_custom_log_level(self):
        """create_logging_middleware should accept custom log level."""
        middleware = create_logging_middleware(log_level="DEBUG")
        assert isinstance(middleware, LoggingMiddleware)
        assert middleware.config.log_level == "DEBUG"

    def test_create_logging_middleware_with_kwargs(self):
        """create_logging_middleware should accept additional kwargs."""
        middleware = create_logging_middleware(skip_paths={"/custom"}, sample_rate=0.1)
        assert isinstance(middleware, LoggingMiddleware)
        assert "/custom" in middleware.config.skip_paths
        assert middleware.config.sample_rate == 0.1

    def test_create_logging_middleware_uses_defaults_when_no_args(self):
        """create_logging_middleware should use defaults when no args provided."""
        middleware = create_logging_middleware()
        assert isinstance(middleware, LoggingMiddleware)
        assert middleware.config is not None


class TestQueueBasedLogging:
    """Test queue-based non-blocking logging setup documented in LOGGING.md."""

    @pytest.fixture(autouse=True)
    def _restore_logging_state(self):
        """Restore logger handlers and module globals that these tests change.

        The tests clear handlers, reset module flags, and stop or start the
        queue listener. Without this fixture, the state leaks to the next test
        in the same worker process.
        """
        saved_loggers = [
            (logger, logger.handlers[:], logger.level, logger.propagate)
            for logger in map(logging.getLogger, ("", "django", "django.server", "django_bolt"))
        ]
        configured = config_module._LOGGING_CONFIGURED
        listener = config_module._QUEUE_LISTENER
        queue = config_module._QUEUE
        listener_running = listener is not None and listener._thread is not None

        yield

        current = config_module._QUEUE_LISTENER
        if current is not None and current is not listener and current._thread is not None:
            current.stop()
        # setup_django_logging(force=True) stops the listener that was running before the test.
        if listener_running and listener._thread is None:
            listener.start()
        config_module._LOGGING_CONFIGURED = configured
        config_module._QUEUE_LISTENER = listener
        config_module._QUEUE = queue
        for logger, handlers, level, propagate in saved_loggers:
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
    def test_a_fork_takes_the_logging_lock_before_the_handler_locks(self):
        """The fork hook takes the locks in the order of ``dictConfig()``: module lock, then handler lock.

        A thread in ``dictConfig()`` holds the module lock and then waits for
        each handler lock. If the fork hook takes a handler lock first, the
        two threads wait for each other with no end. The scenario runs in a
        subprocess, so a deadlock ends at the timeout and not in pytest.
        """
        script = textwrap.dedent(
            """
            import logging, os, sys, threading, time
            from queue import Queue
            import django_bolt.logging.config as config

            handler = logging.StreamHandler(sys.stderr)
            config._QUEUE = Queue()
            config._QUEUE_LISTENER = config._BoundedQueueListener(config._QUEUE, handler)
            held = threading.Event()

            def configure():
                # dictConfig() holds the module lock, then takes each handler lock.
                with logging._lock:
                    held.set()
                    time.sleep(0.3)
                    handler.acquire()
                    handler.release()

            thread = threading.Thread(target=configure)
            thread.start()
            held.wait()
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            os.waitpid(pid, 0)
            thread.join()
            print("forked")
            """
        )
        try:
            result = subprocess.run(
                [sys.executable, "-W", "ignore::DeprecationWarning", "-c", script],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            )
        except subprocess.TimeoutExpired:
            pytest.fail("the fork hook and dictConfig() waited for each other")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "forked"

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
    # The listener thread runs, so Python warns about the fork. The test checks that fork.
    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
    def test_a_fork_waits_for_the_write_of_the_listener(self):
        """A fork during a write of the listener must not copy the lock of the stream in the held state.

        A fork does not reset the lock of a stream. If the child gets the lock in
        the held state, the listener of the child waits for it with no end. The
        lock of ``_LockedStream`` behaves like the buffer lock of ``sys.stderr``.
        """
        in_write = threading.Event()
        finish = threading.Event()

        class _LockedStream:
            def __init__(self):
                self.lock = _thread.allocate_lock()

            def write(self, text):
                with self.lock:
                    if "SLOW" in text:
                        in_write.set()
                        finish.wait(5)

            def flush(self):
                pass

        queue = Queue()
        listener = _BoundedQueueListener(queue, logging.StreamHandler(_LockedStream()))
        config_module._QUEUE = queue
        config_module._QUEUE_LISTENER = listener
        listener.start()
        queue.put(logging.makeLogRecord({"msg": "SLOW"}))
        assert in_write.wait(5)
        # The write ends a short time after the fork starts.
        timer = threading.Timer(0.2, finish.set)
        timer.start()

        pid = os.fork()
        if pid == 0:
            # An error in the child gives exit status 1. The child must not go on to run pytest.
            exit_code = 1
            try:
                restart_queue_listener_after_fork()
                config_module._QUEUE.put(logging.makeLogRecord({"msg": "from the child"}))
                stop_queue_listener()
                exit_code = 0
            finally:
                os._exit(exit_code)

        deadline = time.monotonic() + 5
        status = None
        while status is None and time.monotonic() < deadline:
            done, raw = os.waitpid(pid, os.WNOHANG)
            status = raw if done else None
            time.sleep(0.05)
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        timer.join()
        listener.stop()
        assert status is not None, "the listener of the child waited for the lock of the stream"
        assert os.waitstatus_to_exitcode(status) == 0

    def test_ensure_queue_logging_returns_queue_handler(self):
        """_ensure_queue_logging should return a QueueHandler."""
        handler = _ensure_queue_logging("INFO")
        assert isinstance(handler, QueueHandler), "Must return QueueHandler for non-blocking logging"

    def test_ensure_queue_logging_creates_queue_listener(self):
        """_ensure_queue_logging should create and start a QueueListener."""
        # Reset global state for test
        config_module._QUEUE_LISTENER = None
        config_module._QUEUE = None

        _ensure_queue_logging("INFO")

        # Should have created listener
        assert config_module._QUEUE_LISTENER is not None, "QueueListener should be created"
        assert config_module._QUEUE is not None, "Queue should be created"

    def test_ensure_queue_logging_uses_drop_on_full_handler(self):
        """The bounded queue must use the drop-on-full handler, not a stock QueueHandler.

        A stock QueueHandler routes queue.Full to handleError(), spamming stderr
        under load instead of dropping. The handler must swallow Full instead.
        """
        config_module._QUEUE_LISTENER = None
        config_module._QUEUE = None

        handler = _ensure_queue_logging("INFO")

        assert isinstance(handler, _DropOnFullQueueHandler), (
            "Bounded queue requires _DropOnFullQueueHandler to avoid stderr storms when full"
        )

    def test_drop_on_full_handler_drops_without_traceback_storm(self):
        """A full queue drops records with a terse notice — never a traceback storm.

        Regression: stock QueueHandler.emit() catches queue.Full and calls
        handleError(), which writes a full traceback to stderr per dropped record
        (logging.raiseExceptions defaults to True) — a stderr storm under the
        exact load spike the bounded queue is meant to survive. The drop-on-full
        handler instead drops the record, tracks dropped_count, and emits one
        short line (no traceback) on the first drop.
        """
        bounded = Queue(maxsize=1)
        handler = _DropOnFullQueueHandler(bounded)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="payload",
            args=None,
            exc_info=None,
        )

        # Saturate the queue.
        handler.emit(record)
        assert bounded.full(), "Queue should be full after first emit"

        # Further emits must not raise, must drop, and must not dump a traceback.
        captured = io.StringIO()
        with redirect_stderr(captured):
            for _ in range(5):
                handler.emit(record)

        output = captured.getvalue()
        assert "Traceback" not in output, "Full queue must not dump a traceback per record"
        assert bounded.qsize() == 1, "Overflow records must be dropped, queue unchanged"
        assert handler.dropped_count == 5, "Each overflow record must be counted"
        # Throttled: only the first drop emits a line (interval is 10000).
        assert output.count("\n") == 1, "Drop notice must be throttled, not per-record"
        assert "dropped" in output

    def test_drop_on_full_handler_does_not_warn_until_full(self):
        """No drop notice while the queue has capacity."""
        bounded = Queue(maxsize=4)
        handler = _DropOnFullQueueHandler(bounded)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="payload",
            args=None,
            exc_info=None,
        )
        captured = io.StringIO()
        with redirect_stderr(captured):
            for _ in range(4):
                handler.emit(record)
        assert captured.getvalue() == "", "No notice should be written while capacity remains"
        assert handler.dropped_count == 0

    def test_listener_stops_cleanly_when_queue_saturated(self):
        """QueueListener.stop() must not raise queue.Full on a saturated bounded queue.

        Regression (found via memory soak): the stock QueueListener.enqueue_sentinel()
        uses put_nowait, which raises queue.Full when the bounded queue is full, so
        stop() crashes and the listener thread leaks. _BoundedQueueListener uses a
        blocking put for the sentinel so shutdown is reliable under saturation.
        """

        class _SlowSink(logging.Handler):
            def emit(self, record):
                time.sleep(0.002)  # consumer slower than the producer → queue saturates

        q = Queue(maxsize=5)
        listener = _BoundedQueueListener(q, _SlowSink())
        listener.start()
        handler = _DropOnFullQueueHandler(q)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="payload",
            args=None,
            exc_info=None,
        )
        captured = io.StringIO()
        with redirect_stderr(captured):
            for _ in range(3000):
                handler.emit(record)  # far outpaces the slow sink → drops + full queue
        assert q.qsize() >= 1, "queue should hold a backlog (saturated) before stop"
        assert handler.dropped_count > 0, "saturation should have dropped records"

        # The bug: stock QueueListener.stop() raises queue.Full here. This must not.
        listener.stop()
        assert listener._thread is None, "listener thread should have stopped cleanly"

    def test_queue_size_is_configurable_via_settings(self, settings):
        """BOLT_LOG_QUEUE_SIZE overrides the default 10000 bound."""
        config_module._QUEUE_LISTENER = None
        config_module._QUEUE = None
        settings.BOLT_LOG_QUEUE_SIZE = 123
        _ensure_queue_logging("INFO")
        assert config_module._QUEUE.maxsize == 123

    @pytest.mark.parametrize("bad_value", [0, -1, "10k"], ids=["zero", "negative", "non-int"])
    def test_invalid_queue_size_falls_back_to_bounded_default_with_notice(self, bad_value, settings):
        """A bad BOLT_LOG_QUEUE_SIZE must not silently create an UNBOUNDED queue.

        Queue(maxsize<=0) is unbounded — the exact OOM risk the bound exists to
        prevent — and a non-int value must not be silently ignored (project rule:
        never silently ignore errors). Both cases must fall back to the bounded
        default AND surface a notice on stderr.
        """
        config_module._QUEUE_LISTENER = None
        config_module._QUEUE = None
        settings.BOLT_LOG_QUEUE_SIZE = bad_value
        captured = io.StringIO()
        with redirect_stderr(captured):
            _ensure_queue_logging("INFO")
        assert config_module._QUEUE.maxsize == 10000, (
            f"BOLT_LOG_QUEUE_SIZE={bad_value!r} must fall back to the bounded default, "
            f"got maxsize={config_module._QUEUE.maxsize} (<=0 means unbounded)"
        )
        assert "BOLT_LOG_QUEUE_SIZE" in captured.getvalue(), (
            "misconfiguration must be surfaced on stderr, not silently ignored"
        )

    def test_setup_django_logging_configures_queue_handlers(self):
        """setup_django_logging should configure queue handlers for django loggers."""
        # Configure Django settings for test
        if not settings.configured:
            settings.configure(
                DEBUG=True,
                SECRET_KEY="test-secret-key",
            )

        # Reset global state
        config_module._LOGGING_CONFIGURED = False
        config_module._QUEUE_LISTENER = None
        config_module._QUEUE = None

        # Clear existing handlers
        for logger_name in ("django", "django.server", "django_bolt", ""):
            logger = logging.getLogger(logger_name)
            logger.handlers.clear()

        setup_django_logging(force=True)

        # Check that loggers have queue handlers
        django_logger = logging.getLogger("django")
        django_server_logger = logging.getLogger("django.server")
        django_bolt_logger = logging.getLogger("django_bolt")

        # All should have handlers after setup
        assert len(django_logger.handlers) > 0, "django logger should have handlers"
        assert len(django_server_logger.handlers) > 0, "django.server logger should have handlers"
        assert len(django_bolt_logger.handlers) > 0, "django_bolt logger should have handlers"

        # At least one should be a QueueHandler
        root_logger = logging.getLogger()
        has_queue_handler = any(isinstance(h, QueueHandler) for h in root_logger.handlers)
        assert has_queue_handler, "Root logger should have a QueueHandler"

    def test_setup_django_logging_prints_each_record_one_time(self, capsys):
        """A record of the django logger or of one of its children goes to the console one time."""
        config_module._LOGGING_CONFIGURED = False

        setup_django_logging(force=True)
        logging.getLogger("django.request").warning("one request record")
        logging.getLogger("django.server").warning("one server record")
        logging.getLogger("django_bolt").warning("one bolt record")
        logging.getLogger("another_app").warning("one app record")
        # stop() sends each queued record to the console handler.
        config_module._QUEUE_LISTENER.stop()

        console = capsys.readouterr().err
        assert [
            console.count(text)
            for text in ("one request record", "one server record", "one bolt record", "one app record")
        ] == [1, 1, 1, 1]

    def test_setup_django_logging_respects_explicit_logging_config(self):
        """setup_django_logging should skip setup when LOGGING is explicitly configured."""
        # Reset global state
        config_module._LOGGING_CONFIGURED = False

        django_logger = logging.getLogger("django")

        # conftest configures settings without LOGGING, so set explicit LOGGING here.
        # Count handlers inside the block: Django reconfigures logging when the override exits.
        with override_settings(LOGGING={"version": 1, "disable_existing_loggers": False}):
            handlers_before = django_logger.handlers[:]
            setup_django_logging(force=True)
            handlers_after = django_logger.handlers[:]

        # Should mark as configured
        assert config_module._LOGGING_CONFIGURED is True

        # With explicit LOGGING, we don't modify handlers
        assert len(handlers_after) == len(handlers_before), "Should not modify handlers with explicit LOGGING"
        assert all(after is before for after, before in zip(handlers_after, handlers_before, strict=True)), (
            "Should not replace handlers with explicit LOGGING"
        )

    def test_setup_django_logging_is_idempotent(self):
        """setup_django_logging should not reconfigure when called multiple times."""
        # Configure Django settings for test
        if not settings.configured:
            settings.configure(
                DEBUG=True,
                SECRET_KEY="test-secret-key",
            )

        # First call
        config_module._LOGGING_CONFIGURED = False
        setup_django_logging(force=True)
        first_configured = config_module._LOGGING_CONFIGURED

        # Second call (should be no-op)
        setup_django_logging()
        second_configured = config_module._LOGGING_CONFIGURED

        assert first_configured is True, "First call should configure logging"
        assert second_configured is True, "Second call should preserve configured state"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
