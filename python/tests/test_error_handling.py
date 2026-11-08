"""Tests for Django-Bolt error handling system."""

import json
import logging
from unittest.mock import patch

import django
import msgspec
import pytest
from django.conf import settings  # noqa: PLC0415
from django.core.exceptions import PermissionDenied

from django_bolt import BoltAPI
from django_bolt.error_handlers import (
    BoltError,
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
    BoltException,
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
    ValidationException,
    parse_msgspec_decode_error,
)
from django_bolt.responses import JSON
from django_bolt.testing import TestClient


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

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (NotFound(), "404: Not Found"),
            (HTTPException(400, "bad"), "400: bad"),
            (HTTPException(status_code=404), "404: Not Found"),
            (BadRequest(detail="Missing id"), "400: Missing id"),
            (InternalServerError(), "500: Internal Server Error"),
        ],
    )
    def test_http_exception_str_shows_detail_once(self, exc, expected):
        """Test str() of an HTTPException gives the status and the detail once."""
        assert str(exc) == expected

    def test_http_exception_str_in_traceback_text(self):
        """Test the text of a raised HTTPException has the detail once."""
        with pytest.raises(NotFound, match=r"^404: Not Found$"):
            raise NotFound()

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

    def test_bolt_exception_text_has_the_arguments_and_the_detail(self):
        """The text of an exception is in the log. It has each argument, then the detail."""
        assert str(BoltException("first", "second", detail="why")) == "first second why"
        assert str(BoltException("only")) == "only"
        assert repr(BoltException(detail="why")) == "BoltException - why"

    def test_bolt_exception_subclass_keeps_the_detail_that_it_declares(self):
        class Custom(BoltException):
            detail = "Custom default"

        assert Custom().detail == "Custom default"
        assert str(Custom()) == "Custom default"
        assert Custom("given").detail == "given"
        assert Custom(detail="other").detail == "other"

    def test_http_exception_with_an_empty_detail_uses_the_status_phrase(self):
        assert HTTPException(404, detail=[]).detail == "Not Found"
        assert HTTPException(404, detail="").detail == "Not Found"

    def test_validation_exception_has_a_fixed_detail(self):
        assert ValidationException([1]).detail == "Validation error"
        assert str(ValidationException([1])) == "Validation error"

    def test_request_validation_error_text_joins_the_errors(self):
        """msgspec uses this text for the message of a Serializer validator error."""
        errors = [
            {"loc": ["body", "items", 1], "msg": "too small", "type": "value_error"},
            {"msg": "no location"},
            {"loc": [], "msg": "empty location"},
            {"loc": ["name"]},
            "plain text",
        ]
        assert (
            str(RequestValidationError(errors))
            == "body.items.1: too small; no location; empty location; name: ; plain text"
        )
        assert str(RequestValidationError([])) == "Validation error"

    def test_response_validation_error_text_counts_the_errors(self):
        error = ResponseValidationError([{"loc": ["response", "id"]}, "second"], body={"id": "x"})
        assert str(error) == "2 validation error(s):\n  {'loc': ['response', 'id']}\n  second\n"
        assert error.body == {"id": "x"}


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
        assert data == {"detail": "Response validation error"}

    def test_response_validation_error_handler_debug(self):
        """Debug mode keeps the validation errors in the response."""
        errors = [{"loc": ["response", "id"], "msg": "Field required", "type": "missing"}]
        exc = ResponseValidationError(errors)
        status, headers, body = response_validation_error_handler(exc, debug=True)

        assert status == 500
        data = json.loads(body)
        assert data["detail"] == "Response validation error"
        assert data["extra"]["validation_errors"] == errors

    def test_request_validation_error_handler_puts_each_kind_of_error_under_body(self):
        errors = [
            {"loc": ["body", "a"], "msg": "m", "type": "t"},
            "plain text",
            msgspec.ValidationError("Expected `int` - at `$.x`"),
        ]
        status, _, body = request_validation_error_handler(RequestValidationError(errors))

        assert status == 422
        assert json.loads(body)["detail"] == [
            {"loc": ["body", "a"], "msg": "m", "type": "t"},
            {"loc": ["body"], "msg": "plain text", "type": "validation_error"},
            {"loc": ["body", "x"], "msg": "Expected `int`", "type": "validation_error"},
        ]

    def test_response_validation_error_handler_logs_the_errors_and_the_exception(self, caplog):
        exc = ResponseValidationError(
            [{"loc": ["response", "id"], "msg": "Field required", "type": "missing"}, "plain"]
        )
        # The body has the errors only in debug mode.
        with caplog.at_level(logging.ERROR, logger="django_bolt.error_handlers"):
            status, _, body = response_validation_error_handler(exc, debug=True)

        data = json.loads(body)
        assert status == 500
        assert data["detail"] == "Response validation error"
        assert data["extra"]["validation_errors"] == [
            {"loc": ["response", "id"], "msg": "Field required", "type": "missing"},
            {"loc": ["response"], "msg": "plain", "type": "validation_error"},
        ]
        [record] = caplog.records
        assert record.levelno == logging.ERROR
        assert record.getMessage() == (
            "Response Validation Error: Handler returned invalid response. Errors: "
            "[{'loc': ['response', 'id'], 'msg': 'Field required', 'type': 'missing'}, 'plain']"
        )
        assert record.exc_info[1] is exc

    def test_generic_exception_handler_hides_the_exception_by_default(self):
        """The default is production mode. The body does not tell the client what failed."""
        status, headers, body = generic_exception_handler(ValueError("secret value"))

        assert status == 500
        assert headers == [("content-type", "application/json")]
        assert json.loads(body) == {"detail": "Internal Server Error"}

    def test_generic_exception_handler_logs_the_server_error(self, caplog):
        exc = ValueError("secret value")
        with caplog.at_level(logging.ERROR, logger="django_bolt.error_handlers"):
            generic_exception_handler(exc, debug=False)

        [record] = caplog.records
        assert record.levelno == logging.ERROR
        assert record.getMessage() == "Internal Server Error: ValueError - secret value"
        assert record.exc_info[1] is exc

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

    def test_generic_exception_handler_debug_json_has_the_exception_and_one_traceback_line_per_entry(self):
        """The JSON fallback of debug mode lists the lines of the traceback, with no empty line."""

        # The first line ends with X, and the last line ends with a space. A strip must keep both.
        message = "first X\nsecond "

        def fail():
            raise ValueError(message)

        try:
            fail()
        except ValueError as exc:
            with patch("django_bolt.error_handlers.ExceptionReporter", side_effect=Exception("HTML failed")):
                status, _, body = generic_exception_handler(exc, debug=True)

        data = json.loads(body)
        assert status == 500
        assert data["detail"] == f"ValueError: {message}"
        extra = data["extra"]
        assert (extra["exception"], extra["exception_type"]) == (message, "ValueError")
        lines = extra["traceback"]
        assert lines[0] == "Traceback (most recent call last):"
        assert any(line.strip() == "fail()" for line in lines)
        assert any(line.strip() == "raise ValueError(message)" for line in lines)
        assert lines[-2:] == ["ValueError: first X", "second "]
        assert all(line.strip() and "\n" not in line for line in lines)

    def test_generic_exception_handler_debug_json_logs_the_report_failure_at_debug_level(self, caplog):
        with (
            caplog.at_level(logging.DEBUG, logger="django_bolt.error_handlers"),
            patch("django_bolt.error_handlers.ExceptionReporter", side_effect=Exception("HTML failed")),
        ):
            generic_exception_handler(ValueError("x"), debug=True)

        assert [(r.levelno, r.getMessage()) for r in caplog.records if r.levelno < logging.ERROR] == [
            (
                logging.DEBUG,
                "Failed to generate Django ExceptionReporter HTML. Falling back to JSON traceback format. "
                "Error: HTML failed",
            )
        ]

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

    def test_handle_exception_hides_the_exception_when_django_debug_is_false(self, settings):
        settings.DEBUG = False
        status, headers, body = handle_exception(ValueError("secret value"))

        assert status == 500
        assert headers == [("content-type", "application/json")]
        assert json.loads(body) == {"detail": "Internal Server Error"}

    def test_handle_exception_hides_the_exception_when_django_debug_is_not_set(self, settings):
        del settings.DEBUG
        status, headers, body = handle_exception(ValueError("secret value"))

        assert status == 500
        assert headers == [("content-type", "application/json")]
        assert json.loads(body) == {"detail": "Internal Server Error"}

    def test_handle_exception_answers_a_validation_exception_with_a_422(self):
        errors = [{"loc": ["body", "a"], "msg": "m", "type": "t"}, "plain text"]
        status, _, body = handle_exception(ValidationException(errors))

        assert status == 422
        assert json.loads(body)["detail"] == [
            {"loc": ["body", "a"], "msg": "m", "type": "t"},
            {"loc": ["body"], "msg": "plain text", "type": "validation_error"},
        ]

    @pytest.mark.parametrize(("message", "detail"), [("no access", "no access"), ("", "Permission denied")])
    def test_handle_exception_answers_permission_denied_with_a_403(self, message, detail):
        status, _, body = handle_exception(PermissionDenied(message), debug=False)

        assert status == 403
        assert json.loads(body) == {"detail": detail}

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


class _Inner(msgspec.Struct):
    age: int


class _Outer(msgspec.Struct):
    name: str
    inner: _Inner


class _Holder(msgspec.Struct):
    items: list[_Inner] = []
    by_key: dict[str, _Inner] = {}


class _Renamed(msgspec.Struct, rename={"first_name": "first-name", "last_name": "last-name"}):
    first_name: str
    last_name: str


class _RenamedOuter(msgspec.Struct):
    inner: _Renamed


def _validation_error(body: bytes, type_: type = _Outer) -> msgspec.ValidationError:
    try:
        msgspec.json.decode(body, type=type_)
    except msgspec.ValidationError as error:
        return error
    raise AssertionError("The body must fail validation.")


class TestMsgspecValidationErrorToDict:
    """Each client gets one entry per msgspec error, with a location, a message and a type."""

    def test_wrong_type_has_the_path_of_the_value(self):
        error = _validation_error(b'{"name": "a", "inner": {"age": "x"}}')
        assert msgspec_validation_error_to_dict(error) == [
            {"loc": ["body", "inner", "age"], "msg": "Expected `int`, got `str`", "type": "validation_error"}
        ]

    def test_root_is_the_first_item_of_the_location(self):
        error = _validation_error(b'{"name": "a", "inner": {"age": "x"}}')
        [entry] = msgspec_validation_error_to_dict(error, "response")
        assert entry["loc"] == ["response", "inner", "age"]

    def test_missing_field_names_the_field(self):
        error = _validation_error(b"{}", _Inner)
        assert msgspec_validation_error_to_dict(error) == [
            {"loc": ["body", "age"], "msg": "Object missing required field `age`", "type": "missing_field"}
        ]

    def test_missing_field_of_a_nested_struct_has_the_path_and_the_field_name(self):
        error = _validation_error(b'{"name": "a", "inner": {}}')
        assert msgspec_validation_error_to_dict(error) == [
            {
                "loc": ["body", "inner", "age"],
                "msg": "Object missing required field `age`",
                "type": "missing_field",
            }
        ]

    def test_missing_field_in_a_list_item_has_the_index_in_its_loc(self):
        error = _validation_error(b'{"items": [{"age": 1}, {}]}', _Holder)
        assert msgspec_validation_error_to_dict(error) == [
            {
                "loc": ["body", "items", "1", "age"],
                "msg": "Object missing required field `age`",
                "type": "missing_field",
            }
        ]

    def test_missing_field_in_a_dict_value_has_the_path_of_the_dict(self):
        error = _validation_error(b'{"by_key": {"k": {}}}', _Holder)
        assert msgspec_validation_error_to_dict(error) == [
            {
                "loc": ["body", "by_key", "age"],
                "msg": "Object missing required field `age`",
                "type": "missing_field",
            }
        ]

    def test_missing_field_of_a_nested_struct_uses_the_given_root(self):
        error = _validation_error(b'{"name": "a", "inner": {}}')
        [entry] = msgspec_validation_error_to_dict(error, "response")
        assert (entry["loc"], entry["type"]) == (["response", "inner", "age"], "missing_field")

    def test_missing_field_with_a_renamed_name_keeps_the_encoded_name(self):
        error = _validation_error(b'{"inner": {"first-name": "a"}}', _RenamedOuter)
        assert msgspec_validation_error_to_dict(error) == [
            {
                "loc": ["body", "inner", "last-name"],
                "msg": "Object missing required field `last-name`",
                "type": "missing_field",
            }
        ]

    def test_missing_field_with_no_name_in_the_message(self):
        error = msgspec.ValidationError("Object missing required field")
        assert msgspec_validation_error_to_dict(error) == [
            {"loc": ["body", "unknown"], "msg": "Object missing required field", "type": "missing_field"}
        ]

    def test_error_with_no_path_and_no_missing_field(self):
        error = msgspec.ValidationError("Expected `object`, got `array`")
        assert msgspec_validation_error_to_dict(error) == [
            {"loc": ["body"], "msg": "Expected `object`, got `array`", "type": "validation_error"}
        ]


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


def _decode_error(body: bytes) -> msgspec.DecodeError:
    try:
        msgspec.json.decode(body)
    except msgspec.DecodeError as error:
        return error
    raise AssertionError("The body must be invalid JSON.")


class TestParseMsgspecDecodeError:
    """The 422 body of a malformed JSON request names the line and the column of the fault."""

    def test_error_on_the_first_line(self):
        body = b'{"a" 1}'
        assert parse_msgspec_decode_error(_decode_error(body), body) == {
            "type": "json_invalid",
            "loc": ("body", 1, 6),
            "msg": "Invalid JSON at line 1, column 6: JSON is malformed: expected ':' (byte 5)\n  {\"a\" 1}\n       ^",
            "input": '{"a" 1}',
            "ctx": {
                "line": 1,
                "column": 6,
                "byte_position": 5,
                "error": "JSON is malformed: expected ':' (byte 5)",
            },
        }

    def test_error_after_a_blank_line(self):
        body = b'{"a": 1,\n\n  xyz}'
        error = parse_msgspec_decode_error(_decode_error(body), body)
        assert error["loc"] == ("body", 3, 3)
        assert error["msg"] == (
            "Invalid JSON at line 3, column 3: JSON is malformed: object keys must be strings (byte 12)"
            "\n    xyz}\n    ^"
        )
        assert (error["ctx"]["line"], error["ctx"]["column"], error["ctx"]["byte_position"]) == (3, 3, 12)

    def test_error_at_the_first_byte_of_a_line(self):
        body = b'{"a": 1,\n?'
        error = parse_msgspec_decode_error(_decode_error(body), body)
        assert error["loc"] == ("body", 2, 1)
        assert error["msg"].endswith("\n  ?\n  ^")

    def test_input_is_cut_at_200_characters(self):
        body = b'{"k": "' + b"x" * 250 + b'" ?}'
        error = parse_msgspec_decode_error(_decode_error(body), body)
        assert error["input"] == body.decode()[:200]

    def test_bytes_that_are_not_utf8_are_replaced(self):
        body = b"\xff\xfe"
        error = parse_msgspec_decode_error(_decode_error(body), body)
        assert error["input"] == "��"
        assert error["msg"].endswith("\n  ��\n  ^")

    def test_error_without_a_position(self):
        assert parse_msgspec_decode_error(_decode_error(b""), b"") == {
            "type": "json_invalid",
            "loc": ("body",),
            "msg": "Input data was truncated",
            "input": "",
        }

    def test_bytes_that_are_not_utf8_are_replaced_without_a_position(self):
        error = parse_msgspec_decode_error(msgspec.DecodeError("The input is not valid"), b'{"a": "\xff"')
        assert error["input"] == '{"a": "�"'

    def test_empty_input_with_a_position_gives_an_empty_input(self):
        error = parse_msgspec_decode_error(msgspec.DecodeError("JSON is malformed: invalid character (byte 0)"), b"")
        assert error["loc"] == ("body", 1, 1)
        assert error["input"] == ""

    def test_input_without_a_position_is_cut_at_100_characters(self):
        body = b"[" + b"1," * 150
        error = parse_msgspec_decode_error(_decode_error(body), body)
        assert error["loc"] == ("body",)
        assert error["input"] == body.decode()[:100]
        assert "ctx" not in error

    def test_position_after_the_end_points_after_the_last_character(self):
        """A position past the end of the body gives the column after its last character, one-indexed."""
        error = parse_msgspec_decode_error(
            msgspec.DecodeError("JSON is malformed: invalid character (byte 999)"), b"{}"
        )
        assert error["loc"] == ("body", 1, 3)
        assert error["msg"] == (
            "Invalid JSON at line 1, column 3: JSON is malformed: invalid character (byte 999)\n  {}\n    ^"
        )

    def test_http_422_names_the_line_and_the_column(self):
        api = BoltAPI()

        class Payload(msgspec.Struct):
            name: str

        @api.post("/payload")
        async def payload(body: Payload) -> dict:
            return {}

        with TestClient(api) as client:
            response = client.post("/payload", content=b'{"name": "a",\n  "x": ?}')

        assert response.status_code == 422
        [error] = response.json()["detail"]
        assert error["type"] == "json_invalid"
        assert error["loc"] == ["body", 2, 8]
        assert error["ctx"]["byte_position"] == 21


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class Item(msgspec.Struct):
    id: int


def _bad_response_api(**api_kwargs) -> BoltAPI:
    api = BoltAPI(**api_kwargs)

    @api.get("/async-bad")
    async def async_bad() -> Item:
        return {"id": "not-an-int"}

    @api.get("/sync-bad")
    def sync_bad() -> Item:
        return {"id": "not-an-int"}

    return api


def _validation_log_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "django_bolt.error_handlers" and record.levelno == logging.ERROR
    ]


class TestResponseValidationDisclosure:
    """A response validation 500 shows the validation errors only when DEBUG is on."""

    @pytest.mark.parametrize("path", ["/async-bad", "/sync-bad"])
    def test_debug_off_hides_the_validation_errors(self, settings, caplog, path):
        settings.DEBUG = False
        with TestClient(_bad_response_api()) as client, caplog.at_level(logging.ERROR, logger="django_bolt"):
            response = client.get(path)

        assert response.status_code == 500
        assert response.json() == {"detail": "Response validation error"}
        logged = _validation_log_messages(caplog)
        assert len(logged) == 1
        assert logged[0].startswith("Response Validation Error:")
        assert "Response validation failed for Item" in logged[0]
        assert "Expected `int`, got `str`" in logged[0]

    @pytest.mark.parametrize("path", ["/async-bad", "/sync-bad"])
    def test_debug_on_keeps_the_validation_errors(self, settings, path):
        settings.DEBUG = True
        with TestClient(_bad_response_api()) as client:
            response = client.get(path)

        assert response.status_code == 500
        body = response.json()
        assert body["detail"] == "Response validation error"
        errors = body["extra"]["validation_errors"]
        assert errors[0]["loc"][0] == "response"
        assert "Expected `int`, got `str`" in errors[0]["msg"]

    def test_formatter_gets_no_errors_when_debug_is_off(self, settings, caplog):
        """A formatter can show `error.errors` to the client, so Bolt leaves them out. The log keeps them."""
        settings.DEBUG = False
        seen: list[BoltError] = []

        def formatter(error: BoltError, request) -> JSON:
            seen.append(error)
            return JSON({"kind": error.kind, "errors": error.errors}, status_code=error.status)

        api = _bad_response_api(error_formatter=formatter)
        with TestClient(api) as client, caplog.at_level(logging.ERROR, logger="django_bolt"):
            response = client.get("/async-bad")

        assert response.status_code == 500
        assert response.json() == {"kind": "response_validation", "errors": []}
        assert isinstance(seen[0].exception, ResponseValidationError)
        logged = _validation_log_messages(caplog)
        assert len(logged) == 1
        assert "Response validation failed for Item" in logged[0]

    def test_formatter_gets_the_errors_when_debug_is_on(self, settings):
        settings.DEBUG = True

        def formatter(error: BoltError, request) -> JSON:
            return JSON({"errors": error.errors}, status_code=error.status)

        with TestClient(_bad_response_api(error_formatter=formatter)) as client:
            response = client.get("/async-bad")

        assert response.status_code == 500
        assert "Expected `int`, got `str`" in response.json()["errors"][0]["msg"]
