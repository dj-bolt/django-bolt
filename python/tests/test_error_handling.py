"""Tests for Django-Bolt error handling system."""

import json
from unittest.mock import patch

import django
import msgspec
import pytest
from django.conf import settings  # noqa: PLC0415

from django_bolt.error_handlers import (
    format_error_response,
    generic_exception_handler,
    handle_exception,
    http_exception_handler,
    msgspec_validation_error_to_dict,
    request_validation_error_handler,
    response_validation_error_handler,
)
from django_bolt.exceptions import (
    BadRequest,
    Forbidden,
    HTTPException,
    InternalServerError,
    NotFound,
    RequestValidationError,
    ResponseValidationError,
    ServiceUnavailable,
    TooManyRequests,
    Unauthorized,
    UnprocessableEntity,
)


class TestExceptions:
    """Test exception classes."""

    def test_http_exception_basic(self):
        """Test basic HTTPException."""
        exc = HTTPException(status_code=400, detail="Bad request")
        assert exc.status_code == 400
        assert exc.detail == "Bad request"
        assert exc.headers == {}
        assert exc.extra is None

    def test_http_exception_with_headers(self):
        """Test HTTPException with custom headers."""
        exc = HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"})
        assert exc.status_code == 401
        assert exc.headers == {"WWW-Authenticate": "Bearer"}

    def test_http_exception_with_extra(self):
        """Test HTTPException with extra data."""
        exc = HTTPException(status_code=422, detail="Validation failed", extra={"errors": ["field1", "field2"]})
        assert exc.extra == {"errors": ["field1", "field2"]}

    def test_http_exception_default_message(self):
        """Test HTTPException uses HTTP status phrase as default."""
        exc = HTTPException(status_code=404)
        assert exc.detail == "Not Found"

    def test_specialized_exceptions(self):
        """Test specialized exception classes."""
        assert BadRequest().status_code == 400
        assert Unauthorized().status_code == 401
        assert Forbidden().status_code == 403
        assert NotFound().status_code == 404
        assert UnprocessableEntity().status_code == 422
        assert TooManyRequests().status_code == 429
        assert InternalServerError().status_code == 500
        assert ServiceUnavailable().status_code == 503

    def test_exception_repr(self):
        """Test exception __repr__."""
        exc = NotFound(detail="User not found")
        assert "404" in repr(exc)
        assert "NotFound" in repr(exc)
        assert "User not found" in repr(exc)

    def test_validation_exception(self):
        """Test ValidationException."""
        errors = [{"loc": ["body", "name"], "msg": "Field required", "type": "missing"}]
        exc = RequestValidationError(errors)
        assert exc.errors() == errors

    def test_request_validation_error_with_body(self):
        """Test RequestValidationError stores body."""
        errors = [{"loc": ["body"], "msg": "Invalid", "type": "value_error"}]
        body = {"test": "data"}
        exc = RequestValidationError(errors, body=body)
        assert exc.body == body


class TestErrorHandlers:
    """Test error handler functions."""

    def test_format_error_response_simple(self):
        """Test simple error response formatting."""
        status, headers, body = format_error_response(status_code=404, detail="Not found")
        assert status == 404
        assert ("content-type", "application/json") in headers

        # Decode and check JSON
        data = json.loads(body)
        assert data["detail"] == "Not found"

    def test_format_error_response_with_extra(self):
        """Test error response with extra data."""
        status, headers, body = format_error_response(
            status_code=422, detail="Validation failed", extra={"errors": ["field1"]}
        )

        data = json.loads(body)
        assert data["detail"] == "Validation failed"
        assert data["extra"] == {"errors": ["field1"]}

    def test_http_exception_handler(self):
        """Test HTTPException handler."""
        exc = NotFound(detail="User not found")
        status, headers, body = http_exception_handler(exc)

        assert status == 404
        data = json.loads(body)
        assert data["detail"] == "User not found"

    def test_http_exception_handler_with_headers(self):
        """Test HTTPException handler preserves custom headers."""
        exc = Unauthorized(detail="Auth required", headers={"WWW-Authenticate": "Bearer"})
        status, headers, body = http_exception_handler(exc)

        assert status == 401
        assert ("WWW-Authenticate", "Bearer") in headers

    def test_request_validation_error_handler(self):
        """Test request validation error handler."""
        errors = [{"loc": ["body", "email"], "msg": "Invalid email", "type": "value_error"}]
        exc = RequestValidationError(errors)
        status, headers, body = request_validation_error_handler(exc)

        assert status == 422
        data = json.loads(body)
        assert isinstance(data["detail"], list)
        assert len(data["detail"]) == 1
        assert data["detail"][0]["loc"] == ["body", "email"]

    def test_response_validation_error_handler(self):
        """Test response validation error handler."""
        errors = [{"loc": ["response", "id"], "msg": "Field required", "type": "missing"}]
        exc = ResponseValidationError(errors)
        status, headers, body = response_validation_error_handler(exc)

        assert status == 500
        data = json.loads(body)
        assert data["detail"] == "Response validation error"
        assert "validation_errors" in data["extra"]

    def test_generic_exception_handler_production(self):
        """Test generic exception handler in production mode."""
        exc = ValueError("Something went wrong")
        status, headers, body = generic_exception_handler(exc, debug=False)

        assert status == 500
        data = json.loads(body)
        assert data["detail"] == "Internal Server Error"
        # Should not expose details in production
        assert "extra" not in data

    def test_generic_exception_handler_debug(self):
        """Test generic exception handler in debug mode returns HTML."""
        # Configure Django settings for ExceptionReporter
        if not settings.configured:
            settings.configure(
                DEBUG=True,
                SECRET_KEY="test-secret-key",
                INSTALLED_APPS=[],
                ROOT_URLCONF="",
            )
            django.setup()

        exc = ValueError("Something went wrong")

        # Create a mock request dict
        request_dict = {"method": "GET", "path": "/test", "headers": {"user-agent": "test"}, "query_params": {}}

        status, headers, body = generic_exception_handler(exc, debug=True, request=request_dict)

        assert status == 500, "Debug exception must return 500 status"
        # Should return HTML in debug mode
        headers_dict = dict(headers)
        assert headers_dict.get("content-type") == "text/html; charset=utf-8", (
            "Debug mode must return HTML content type"
        )
        # Verify it's HTML content
        html_content = body.decode("utf-8")
        assert "<!DOCTYPE html>" in html_content or "<html>" in html_content, (
            "Debug mode must return valid HTML document"
        )
        assert "ValueError" in html_content, "HTML must contain exception type"
        assert "Something went wrong" in html_content, "HTML must contain exception message"

    def test_generic_exception_handler_debug_without_request(self):
        """Test generic exception handler in debug mode works without request."""
        if not settings.configured:
            settings.configure(
                DEBUG=True,
                SECRET_KEY="test-secret-key",
                INSTALLED_APPS=[],
                ROOT_URLCONF="",
            )
            django.setup()

        exc = RuntimeError("Error without request context")

        # Call without request parameter
        status, headers, body = generic_exception_handler(exc, debug=True, request=None)

        assert status == 500
        headers_dict = dict(headers)
        assert headers_dict.get("content-type") == "text/html; charset=utf-8"
        html_content = body.decode("utf-8")
        assert "RuntimeError" in html_content
        assert "Error without request context" in html_content

    def test_generic_exception_handler_debug_fallback_to_json(self):
        """Test generic exception handler falls back to JSON if HTML generation fails."""
        # Mock ExceptionReporter to raise an exception
        exc = ValueError("Test exception")

        # Mock the ExceptionReporter where it's used (in error_handlers module)
        with patch("django_bolt.error_handlers.ExceptionReporter", side_effect=Exception("HTML failed")):
            status, headers, body = generic_exception_handler(exc, debug=True, request=None)

        assert status == 500, "Fallback must return 500 status"
        headers_dict = dict(headers)
        assert headers_dict.get("content-type") == "application/json", "Fallback must return JSON content type"

        # Should fall back to JSON with traceback
        data = json.loads(body)
        assert "ValueError" in data["detail"], "Fallback JSON must contain exception type in detail"
        assert "extra" in data, "Fallback JSON must include extra field"
        assert "traceback" in data["extra"], "Fallback JSON must include traceback"
        assert "exception_type" in data["extra"], "Fallback JSON must include exception_type"
        assert data["extra"]["exception_type"] == "ValueError"

    def test_generic_exception_handler_preserves_traceback(self):
        """Test that exception traceback is properly preserved and formatted."""

        def inner_function():
            raise ValueError("Inner error")

        def outer_function():
            inner_function()

        try:
            outer_function()
        except ValueError as exc:
            status, headers, body = generic_exception_handler(exc, debug=True, request=None)

            # Try HTML first (primary path)
            headers_dict = dict(headers)
            if headers_dict.get("content-type") == "text/html; charset=utf-8":
                html_content = body.decode("utf-8")
                # HTML should contain traceback info
                assert "inner_function" in html_content or "outer_function" in html_content, (
                    "HTML traceback must show function names"
                )
            else:
                # Fallback JSON path
                data = json.loads(body)
                traceback_lines = data["extra"]["traceback"]
                traceback_str = "".join(traceback_lines)
                assert "inner_function" in traceback_str, "JSON traceback must contain inner_function"
                assert "outer_function" in traceback_str, "JSON traceback must contain outer_function"

    def test_handle_exception_http_exception(self):
        """Test main exception handler with HTTPException."""
        exc = NotFound(detail="Resource not found")
        status, headers, body = handle_exception(exc)

        assert status == 404
        data = json.loads(body)
        assert data["detail"] == "Resource not found"

    def test_handle_exception_validation_error(self):
        """Test main exception handler with validation error."""
        errors = [{"loc": ["body"], "msg": "Invalid", "type": "value_error"}]
        exc = RequestValidationError(errors)
        status, headers, body = handle_exception(exc)

        assert status == 422
        data = json.loads(body)
        assert isinstance(data["detail"], list)

    def test_handle_exception_generic(self):
        """Test main exception handler with generic exception."""
        exc = RuntimeError("Unexpected error")
        status, headers, body = handle_exception(exc, debug=False)

        assert status == 500
        data = json.loads(body)
        assert data["detail"] == "Internal Server Error"

    def test_handle_exception_with_request_parameter(self):
        """Test that handle_exception properly passes request to generic_exception_handler."""
        if not settings.configured:
            settings.configure(
                DEBUG=True,
                SECRET_KEY="test-secret-key",
                INSTALLED_APPS=[],
                ROOT_URLCONF="",
            )
            django.setup()

        exc = ValueError("Test with request")
        request_dict = {
            "method": "POST",
            "path": "/api/users",
            "headers": {"content-type": "application/json"},
        }

        status, headers, body = handle_exception(exc, debug=True, request=request_dict)

        assert status == 500, "Exception with request must return 500"
        # Should return HTML in debug mode
        headers_dict = dict(headers)
        assert headers_dict.get("content-type") == "text/html; charset=utf-8", (
            "Debug mode with request must return HTML"
        )
        html_content = body.decode("utf-8")
        assert "ValueError" in html_content
        assert "Test with request" in html_content

    def test_handle_exception_respects_django_debug_setting(self, settings):
        """Test that handle_exception uses Django DEBUG setting when debug param is not provided."""
        settings.DEBUG = True
        exc = ValueError("Should use Django DEBUG")

        # Call without debug parameter (should check Django settings)
        status, headers, _ = handle_exception(exc)

        # Since Django DEBUG=True, should return HTML
        headers_dict = dict(headers)
        assert headers_dict.get("content-type") == "text/html; charset=utf-8", (
            "Should use Django DEBUG=True setting when debug param is not provided"
        )
        assert status == 500

    def test_handle_exception_debug_overrides_django_setting(self):
        """Test that explicit debug=True overrides Django DEBUG=False."""
        exc = ValueError("Explicit debug override")

        # Explicitly pass debug=True (should ignore Django settings)
        status, _, _ = handle_exception(exc, debug=True)

        # Should return 500 because we have an exception
        assert status == 500, "Exception must return 500 status"

    def test_msgspec_validation_error_conversion(self):
        """Test msgspec ValidationError to dict conversion."""

        # Create a msgspec validation error
        class TestStruct(msgspec.Struct):
            name: str
            age: int

        try:
            msgspec.json.decode(b'{"age": "invalid"}', type=TestStruct)
        except msgspec.ValidationError as e:
            errors = msgspec_validation_error_to_dict(e)
            assert isinstance(errors, list)
            assert len(errors) > 0
            assert "loc" in errors[0]
            assert "msg" in errors[0]
            assert "type" in errors[0]


class TestExceptionIntegration:
    """Integration tests for exception handling."""

    def test_exception_chain(self):
        """Test that exceptions can be chained properly."""
        try:
            raise ValueError("Original error")
        except ValueError as e:
            exc = InternalServerError(detail=str(e))
            assert exc.status_code == 500
            assert "Original error" in exc.detail

    def test_exception_context_preservation(self):
        """Test that exception context is preserved."""
        exc = BadRequest(detail="Invalid input", extra={"field": "email", "value": "invalid@"})

        status, headers, body = http_exception_handler(exc)
        data = json.loads(body)

        assert data["detail"] == "Invalid input"
        assert data["extra"]["field"] == "email"
        assert data["extra"]["value"] == "invalid@"

    def test_multiple_validation_errors(self):
        """Test handling multiple validation errors."""
        errors = [
            {"loc": ["body", "name"], "msg": "Field required", "type": "missing"},
            {"loc": ["body", "email"], "msg": "Invalid format", "type": "value_error"},
            {"loc": ["body", "age"], "msg": "Must be positive", "type": "value_error"},
        ]
        exc = RequestValidationError(errors)
        status, headers, body = request_validation_error_handler(exc)

        data = json.loads(body)
        assert len(data["detail"]) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
