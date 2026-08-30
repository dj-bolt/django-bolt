"""Error handlers for Django-Bolt.

Provides default exception handlers that convert Python exceptions into
structured HTTP error responses.
"""

import logging
import re
import traceback
from typing import Any

import msgspec

from . import _json
from .exceptions import (
    HTTPException,
    RequestValidationError,
    ResponseValidationError,
    ValidationException,
)

logger = logging.getLogger(__name__)

# Pre-computed constant headers (avoid per-error allocation)
_DEFAULT_JSON_HEADERS: list[tuple[str, str]] = [("content-type", "application/json")]

# Django import - may fail if Django not configured
try:
    from django.conf import settings as django_settings
    from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
    from django.http import HttpResponse as DjangoHttpResponse
    from django.views.debug import ExceptionReporter
except ImportError:
    django_settings = None
    DjangoPermissionDenied = None
    DjangoHttpResponse = None
    ExceptionReporter = None


def _json_response(
    status_code: int, headers: dict[str, str] | None, payload: Any
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Encode ``payload`` as a JSON response tuple with optional extra headers."""
    if headers:
        response_headers = list(_DEFAULT_JSON_HEADERS)
        response_headers.extend(headers.items())
    else:
        response_headers = _DEFAULT_JSON_HEADERS
    return status_code, response_headers, _json.encode(payload)


def format_error_response(
    status_code: int,
    detail: Any,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | list[Any] | None = None,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Format an error response.

    Args:
        status_code: HTTP status code
        detail: Error detail (string or structured data)
        headers: Optional HTTP headers
        extra: Optional extra data to include in response

    Returns:
        Tuple of (status_code, headers, body)
    """
    error_body: dict[str, Any] = {"detail": detail}
    if extra is not None:
        error_body["extra"] = extra
    return _json_response(status_code, headers, error_body)


def serialize_django_response(response: Any) -> tuple[int, list[tuple[str, str]], bytes]:
    """Convert Django HttpResponse to response tuple.

    This is called by middleware adapter when Django middleware returns an
    HttpResponse (e.g., redirects from @login_required, CSRF failures, etc.).

    Args:
        response: Django HttpResponse instance

    Returns:
        Tuple of (status_code, headers, body)
    """
    status_code = response.status_code

    # Extract headers from Django response
    headers: list[tuple[str, str]] = []
    for header, value in response.items():
        headers.append((header.lower(), value))

    # Get response content
    if hasattr(response, "content"):
        body = response.content
        if isinstance(body, str):
            body = body.encode("utf-8")
    else:
        body = b""

    return status_code, headers, body


def is_django_response(obj: Any) -> bool:
    """Check if object is a Django HttpResponse.

    This avoids importing Django in hot paths - only checks when we suspect
    we have a Django response.

    Args:
        obj: Object to check

    Returns:
        True if obj is a Django HttpResponse
    """
    if DjangoHttpResponse is None:
        return False
    return isinstance(obj, DjangoHttpResponse)


def http_exception_handler(exc: HTTPException) -> tuple[int, list[tuple[str, str]], bytes]:
    """Convert an HTTPException to a response tuple.

    A typed ``body`` is encoded as-is. Otherwise the ``detail`` envelope is used.
    """
    if exc.body is not None:
        return _json_response(exc.status_code, exc.headers, exc.body)
    return format_error_response(
        status_code=exc.status_code,
        detail=exc.detail,
        headers=exc.headers,
        extra=exc.extra,
    )


# Pre-compiled regex patterns for validation error parsing
_RE_AT_LOCATION = re.compile(r" - at `(.+?)`$")
_RE_FIELD_NAME = re.compile(r"`(\w+)`")
_RE_PATH_SPLIT = re.compile(r"[.\[]")


def _loc_segments(path: str) -> list[str]:
    """Split a msgspec path like ``$.items[1].age`` into ``["items", "1", "age"]``."""
    return [seg for seg in _RE_PATH_SPLIT.split(path.replace("]", "")) if seg and seg != "$"]


def msgspec_validation_error_to_dict(error: msgspec.ValidationError) -> list[dict[str, Any]]:
    """Convert msgspec ValidationError to structured error list.

    Args:
        error: msgspec.ValidationError instance

    Returns:
        List of error dictionaries with 'loc', 'msg', 'type' fields
    """
    error_msg = str(error)

    # A Serializer's own validators raise RequestValidationError from
    # __post_init__, one entry per failing field. msgspec catches that during
    # decoding and re-raises a plain ValidationError whose message is str(exc)
    # (every field joined with "; ") plus " - at `$.path`" when the struct is
    # nested. The original survives on __cause__: keep its per-field entries
    # and prefix each loc with the container path msgspec appended.
    cause = error.__cause__
    if isinstance(cause, RequestValidationError):
        suffix = error_msg[len(str(cause)) :]
        loc_match = _RE_AT_LOCATION.search(suffix)
        prefix = _loc_segments(loc_match.group(1)) if loc_match else []
        return [{**err, "loc": ["body", *prefix, *err["loc"][1:]]} for err in cause.errors()]

    # Try to extract field location with pre-compiled regex
    loc_match = _RE_AT_LOCATION.search(error_msg)
    if loc_match:
        msg_part = error_msg[: loc_match.start()]
        loc_parts = ["body", *_loc_segments(loc_match.group(1))]
        return [{"loc": loc_parts, "msg": msg_part, "type": "validation_error"}]

    if "missing required field" in error_msg.lower():
        match = _RE_FIELD_NAME.search(error_msg)
        field = match.group(1) if match else "unknown"
        return [{"loc": ["body", field], "msg": error_msg, "type": "missing_field"}]

    return [{"loc": ["body"], "msg": error_msg, "type": "validation_error"}]


def request_validation_error_handler(
    exc: RequestValidationError,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Handle RequestValidationError and convert to 422 response.

    Args:
        exc: RequestValidationError instance

    Returns:
        Tuple of (status_code, headers, body)
    """
    errors = exc.errors()

    # Convert errors to structured format if needed
    formatted_errors = []
    for error in errors:
        if isinstance(error, dict):
            formatted_errors.append(error)
        elif isinstance(error, msgspec.ValidationError):
            formatted_errors.extend(msgspec_validation_error_to_dict(error))
        else:
            # Generic error
            formatted_errors.append(
                {
                    "loc": ["body"],
                    "msg": str(error),
                    "type": "validation_error",
                }
            )

    return format_error_response(
        status_code=422,
        detail=formatted_errors,
    )


def response_validation_error_handler(
    exc: ResponseValidationError,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Handle ResponseValidationError and convert to 500 response.

    Args:
        exc: ResponseValidationError instance

    Returns:
        Tuple of (status_code, headers, body)
    """
    # Log response validation errors - these indicate bugs in handler return values
    errors = exc.errors()
    logger.error(
        "Response Validation Error: Handler returned invalid response. Errors: %s",
        errors,
        exc_info=exc,
    )

    formatted_errors = []
    for error in errors:
        if isinstance(error, dict):
            formatted_errors.append(error)
        elif isinstance(error, msgspec.ValidationError):
            formatted_errors.extend(msgspec_validation_error_to_dict(error))
        else:
            formatted_errors.append(
                {
                    "loc": ["response"],
                    "msg": str(error),
                    "type": "validation_error",
                }
            )

    return format_error_response(
        status_code=500,
        detail="Response validation error",
        extra={"validation_errors": formatted_errors},
    )


def generic_exception_handler(
    exc: Exception,
    debug: bool = False,
    request: Any | None = None,  # noqa: ARG001 - kept for API compatibility
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Handle generic exceptions and convert to 500 response.

    Args:
        exc: Exception instance
        debug: Whether to include traceback in response
        request: Optional request dict or Django request object for ExceptionReporter

    Returns:
        Tuple of (status_code, headers, body)
    """
    # ALWAYS log 500 errors - these are unexpected server errors
    logger.error(
        "Internal Server Error: %s - %s",
        type(exc).__name__,
        str(exc),
        exc_info=exc,  # This includes full traceback in logs
    )

    detail = "Internal Server Error"
    extra = None

    if debug:
        # Try to use Django's ExceptionReporter HTML page
        try:
            if ExceptionReporter is not None:
                # ExceptionReporter works fine with None request (avoids URL resolution issues)
                reporter = ExceptionReporter(None, type(exc), exc, exc.__traceback__)
                html_content = reporter.get_traceback_html()

                # Return HTML response instead of JSON
                return (500, [("content-type", "text/html; charset=utf-8")], html_content.encode("utf-8"))
        except Exception as e:
            # Fallback to standard traceback formatting in JSON
            logger.debug(
                "Failed to generate Django ExceptionReporter HTML. Falling back to JSON traceback format. Error: %s", e
            )

        # Fallback to JSON with traceback
        tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        # Split into individual lines for better JSON display, stripping trailing newlines
        tb_formatted = [line.rstrip("\n") for line in "".join(tb_lines).split("\n") if line.strip()]
        extra = {
            "exception": str(exc),
            "exception_type": type(exc).__name__,
            "traceback": tb_formatted,
        }
        detail = f"{type(exc).__name__}: {str(exc)}"

    return format_error_response(
        status_code=500,
        detail=detail,
        extra=extra,
    )


def handle_exception(
    exc: Exception,
    debug: bool | None = None,
    request: Any | None = None,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Main exception handler that routes to specific handlers.

    Args:
        exc: Exception instance
        debug: Whether to include debug information. If None, will check Django DEBUG setting.
              If explicitly False, will not show debug info even if Django DEBUG=True.
        request: Optional request object for Django ExceptionReporter

    Returns:
        Tuple of (status_code, headers, body)
    """
    # Check Django's DEBUG setting dynamically only if debug is not explicitly set
    if debug is None:
        try:
            debug = django_settings.DEBUG if django_settings and django_settings.configured else False
        except (ImportError, AttributeError):
            debug = False

    if isinstance(exc, HTTPException):
        return http_exception_handler(exc)
    elif isinstance(exc, RequestValidationError):
        return request_validation_error_handler(exc)
    elif isinstance(exc, ResponseValidationError):
        return response_validation_error_handler(exc)
    elif isinstance(exc, ValidationException):
        # Generic validation exception
        return request_validation_error_handler(RequestValidationError(exc.errors()))
    elif isinstance(exc, msgspec.ValidationError):
        # Direct msgspec validation error
        errors = msgspec_validation_error_to_dict(exc)
        return format_error_response(
            status_code=422,
            detail=errors,
        )
    elif isinstance(exc, FileNotFoundError):
        # FileNotFoundError from FileResponse - return 404
        return format_error_response(
            status_code=404,
            detail=str(exc) or "File not found",
        )
    elif isinstance(exc, PermissionError):
        # PermissionError from FileResponse path validation - return 403
        return format_error_response(
            status_code=403,
            detail=str(exc) or "Permission denied",
        )
    elif DjangoPermissionDenied is not None and isinstance(exc, DjangoPermissionDenied):
        # Django's PermissionDenied exception - return 403
        return format_error_response(
            status_code=403,
            detail=str(exc) or "Permission denied",
        )
    else:
        # Generic exception
        return generic_exception_handler(exc, debug=debug, request=request)
