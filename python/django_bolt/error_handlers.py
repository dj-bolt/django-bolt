"""Error handlers for Django-Bolt.

Provides default exception handlers that convert Python exceptions into
structured HTTP error responses.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import msgspec

from . import _json
from .exceptions import (
    HTTPException,
    RequestValidationError,
    ResponseFileNotAllowed,
    ResponseFileNotFound,
    ResponseValidationError,
    ValidationException,
)
from .responses import HTML, JSON, PlainText, Response
from .serialization import _render_response_body

logger = logging.getLogger(__name__)

# Pre-computed constant headers (avoid per-error allocation)
_DEFAULT_JSON_HEADERS: list[tuple[str, str]] = [("content-type", "application/json")]

# Django import - may fail if Django not configured
try:
    from django.conf import settings as django_settings
    from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
    from django.core.exceptions import SuspiciousOperation as DjangoSuspiciousOperation
    from django.http import Http404 as DjangoHttp404
    from django.http import HttpResponse as DjangoHttpResponse
    from django.views.debug import ExceptionReporter
except ImportError:
    django_settings = None
    DjangoPermissionDenied = None
    DjangoSuspiciousOperation = None
    DjangoHttp404 = None
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


def msgspec_validation_error_to_dict(error: msgspec.ValidationError, root: str = "body") -> list[dict[str, Any]]:
    """Convert msgspec ValidationError to structured error list.

    Args:
        error: msgspec.ValidationError instance
        root: The first item of each 'loc'. A request error has "body", and a
            response error has "response".

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
        return [{**err, "loc": [root, *prefix, *err["loc"][1:]]} for err in cause.errors()]

    # Try to extract field location with pre-compiled regex
    loc_match = _RE_AT_LOCATION.search(error_msg)
    if loc_match:
        msg_part = error_msg[: loc_match.start()]
        loc_parts = [root, *_loc_segments(loc_match.group(1))]
        return [{"loc": loc_parts, "msg": msg_part, "type": "validation_error"}]

    if "missing required field" in error_msg.lower():
        match = _RE_FIELD_NAME.search(error_msg)
        field = match.group(1) if match else "unknown"
        return [{"loc": [root, field], "msg": error_msg, "type": "missing_field"}]

    return [{"loc": [root], "msg": error_msg, "type": "validation_error"}]


def _format_validation_errors(errors: Any, loc: str) -> list[dict[str, Any]]:
    """Convert the errors of a validation exception to dicts with 'loc', 'msg' and 'type'.

    ``loc`` is the first item of the location of each error, and the whole
    location of an error that does not give one.
    """
    formatted_errors = []
    for error in errors:
        if isinstance(error, dict):
            formatted_errors.append(error)
        elif isinstance(error, msgspec.ValidationError):
            formatted_errors.extend(msgspec_validation_error_to_dict(error, loc))
        else:
            formatted_errors.append({"loc": [loc], "msg": str(error), "type": "validation_error"})
    return formatted_errors


def _log_response_validation_error(exc: ResponseValidationError, errors: list[dict[str, Any]]) -> None:
    # Response validation errors show bugs in the return values of handlers.
    logger.error(
        "Response Validation Error: Handler returned invalid response. Errors: %s",
        errors,
        exc_info=exc,
    )


def _log_suspicious_operation(exc: BaseException) -> None:
    # Django logs a SuspiciousOperation to django.security.<class name>, for
    # example DisallowedHost from request.build_absolute_uri().
    logging.getLogger(f"django.security.{exc.__class__.__name__}").error(
        str(exc), exc_info=exc, extra={"status_code": 400}
    )


def _log_server_error(exc: BaseException) -> None:
    # Always log 500 errors. They are unexpected server errors.
    logger.error(
        "Internal Server Error: %s - %s",
        type(exc).__name__,
        str(exc),
        exc_info=exc,
    )


def request_validation_error_handler(
    exc: RequestValidationError,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Handle RequestValidationError and convert to 422 response.

    Args:
        exc: RequestValidationError instance

    Returns:
        Tuple of (status_code, headers, body)
    """
    return format_error_response(
        status_code=422,
        detail=_format_validation_errors(exc.errors(), "body"),
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
    _log_response_validation_error(exc, exc.errors())
    return format_error_response(
        status_code=500,
        detail="Response validation error",
        extra={"validation_errors": _format_validation_errors(exc.errors(), "response")},
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
    _log_server_error(exc)

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


_CLIENT_ERROR_TYPES: tuple[type[BaseException], ...] = tuple(
    error_type
    for error_type in (
        ValidationException,
        msgspec.ValidationError,
        ResponseFileNotFound,
        ResponseFileNotAllowed,
        DjangoPermissionDenied,
        DjangoSuspiciousOperation,
        DjangoHttp404,
    )
    if error_type is not None
)


def is_server_exception(exc: BaseException) -> bool:
    """Return True when ``handle_exception`` answers ``exc`` with a 5xx.

    The exception log uses this answer. An ``error_formatter`` can change the
    status of the response, but not this answer. Thus a formatter cannot hide a
    server error from the log.
    """
    if isinstance(exc, HTTPException):
        return exc.status_code >= 500
    if isinstance(exc, ResponseValidationError):
        return True
    return not isinstance(exc, _CLIENT_ERROR_TYPES)


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
    elif isinstance(exc, ResponseFileNotFound):
        # The text of the error has the path of the file. The client must not get it.
        return format_error_response(status_code=404, detail="File not found")
    elif isinstance(exc, ResponseFileNotAllowed):
        return format_error_response(status_code=403, detail="Permission denied")
    elif DjangoSuspiciousOperation is not None and isinstance(exc, DjangoSuspiciousOperation):
        # Django answers 400 and logs the exception to django.security.
        _log_suspicious_operation(exc)
        return format_error_response(
            status_code=400,
            detail=str(exc) if debug else "Bad Request",
        )
    elif DjangoPermissionDenied is not None and isinstance(exc, DjangoPermissionDenied):
        # Django's PermissionDenied exception - return 403
        return format_error_response(
            status_code=403,
            detail=str(exc) or "Permission denied",
        )
    elif DjangoHttp404 is not None and isinstance(exc, DjangoHttp404):
        # Django answers 404. It shows the message only in debug mode, because
        # the message of get_object_or_404 names the model.
        return format_error_response(status_code=404, detail=(str(exc) or "Not Found") if debug else "Not Found")
    else:
        # Generic exception
        return generic_exception_handler(exc, debug=debug, request=request)


# ---------------------------------------------------------------------------
# Error formatter: BoltAPI(error_formatter=...)
# ---------------------------------------------------------------------------

# The kind of an HTTPException comes from its status. Another status gives "http_error".
# The Rust core uses the same kinds for the errors that it builds.
_KIND_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    422: "validation",
    429: "rate_limited",
    500: "internal_error",
    501: "not_implemented",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}

# The RFC 9110 names. Python 3.12 has older names for these, and the title must not change with Python.
_STATUS_TITLES: dict[int, str] = {
    **{status.value: status.phrase for status in HTTPStatus},
    413: "Content Too Large",
    414: "URI Too Long",
    416: "Range Not Satisfiable",
    422: "Unprocessable Content",
}

_VALIDATION_DETAIL = "Request validation failed"

# RFC 9110 token characters for a header name, and the characters that a header value must not have.
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_HEADER_VALUE_FORBIDDEN = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


class BoltError(msgspec.Struct, frozen=True, kw_only=True):
    """An error response that the framework makes, given to the error formatter.

    Attributes:
        status: The HTTP status code.
        kind: A stable name for the error, for example "not_authenticated",
            "forbidden", "not_found", "validation", "rate_limited",
            "payload_too_large", "response_validation" or "internal_error".
        detail: A message for a human. Bolt does not put internal data, such as a file path, in it.
        errors: The field errors. Each one is a dict with "loc", "msg" and "type", and some have "ctx".
        headers: The headers that the error needs, for example WWW-Authenticate
            or Retry-After. Bolt adds them to the response of the formatter.
        extra: The ``extra`` of an HTTPException, or ``{"retry_after": n}`` for
            a rate limit.
        exception: The Python exception. It is None for an error of the Rust core.
    """

    status: int
    kind: str
    detail: str
    errors: list[dict[str, Any]] = msgspec.field(default_factory=list)
    headers: dict[str, str] = msgspec.field(default_factory=dict)
    extra: Any = None
    exception: BaseException | None = None


ErrorFormatter = Callable[[BoltError, Any], Any]


class _ErrorSchema(msgspec.Struct, frozen=True):
    """The error body of a formatter, as the OpenAPI schema shows it."""

    schema: Any
    media_type: str


def error_schema[F: Callable[..., Any]](schema: Any, *, media_type: str = "application/json") -> Callable[[F], F]:
    """Declare the error body of a formatter for the OpenAPI schema.

    Use it as a decorator on the formatter. ``schema`` is the type of the body,
    for example a ``msgspec.Struct``. The generated error responses then show
    this type and ``media_type``. Without this decorator, they show no body.
    """

    def decorate(formatter: F) -> F:
        formatter.__bolt_error_schema__ = _ErrorSchema(schema, media_type)
        return formatter

    return decorate


# The docstrings of these structs are the descriptions in the OpenAPI schema.
class ProblemFieldError(msgspec.Struct, kw_only=True):
    """A field error: the location of the value, a message and the error type."""

    loc: list[str | int]
    msg: str
    type: str


class ProblemDetails(msgspec.Struct, kw_only=True):
    """An error body in the RFC 9457 Problem Details format.

    The body has "errors" when the error has field errors. The keys of the
    "extra" of the error are more members of the body.
    """

    type: str
    title: str
    status: int
    detail: str
    instance: str
    errors: list[ProblemFieldError] = []


@error_schema(ProblemDetails, media_type="application/problem+json")
def problem_details(error: BoltError, request: Any) -> Response:
    """Format an error as RFC 9457 Problem Details (``application/problem+json``).

    Use it as ``BoltAPI(error_formatter=problem_details)``. The body has
    "type", "title", "status", "detail" and "instance". It has "errors" when
    the error has field errors. The keys of ``error.extra`` are extension
    members. A key with the name of a standard member does not replace it.
    An "errors" key is not used, because "errors" holds only field errors.
    """
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": _STATUS_TITLES.get(error.status, "Error"),
        "status": error.status,
        "detail": error.detail,
        "instance": request.path,
    }
    if error.errors:
        body["errors"] = error.errors
    if isinstance(error.extra, dict):
        for key, value in error.extra.items():
            # "errors" is a list of field errors, as ProblemDetails declares it.
            if key != "errors":
                body.setdefault(key, value)
    elif error.extra is not None:
        body.setdefault("extra", error.extra)
    return Response(body, status_code=error.status, media_type="application/problem+json")


def bolt_error_from_exception(exc: BaseException) -> BoltError:
    """Describe the error response for an exception, as ``handle_exception`` sends it."""
    if isinstance(exc, HTTPException):
        return BoltError(
            status=exc.status_code,
            kind=_KIND_BY_STATUS.get(exc.status_code, "http_error"),
            detail=exc.detail,
            headers=dict(exc.headers),
            extra=exc.extra,
            exception=exc,
        )
    if isinstance(exc, ResponseValidationError):
        return BoltError(
            status=500,
            kind="response_validation",
            detail="Response validation error",
            errors=_format_validation_errors(exc.errors(), "response"),
            exception=exc,
        )
    if isinstance(exc, ValidationException):
        errors = _format_validation_errors(exc.errors(), "body")
    elif isinstance(exc, msgspec.ValidationError):
        errors = msgspec_validation_error_to_dict(exc)
    elif isinstance(exc, ResponseFileNotFound):
        # The text of the error has the path of the file. The client must not get it.
        return BoltError(status=404, kind="not_found", detail="File not found", exception=exc)
    elif isinstance(exc, ResponseFileNotAllowed):
        return BoltError(status=403, kind="forbidden", detail="Permission denied", exception=exc)
    elif DjangoPermissionDenied is not None and isinstance(exc, DjangoPermissionDenied):
        return BoltError(status=403, kind="forbidden", detail=str(exc) or "Permission denied", exception=exc)
    elif DjangoSuspiciousOperation is not None and isinstance(exc, DjangoSuspiciousOperation):
        return BoltError(status=400, kind="bad_request", detail="Bad Request", exception=exc)
    elif DjangoHttp404 is not None and isinstance(exc, DjangoHttp404):
        return BoltError(status=404, kind="not_found", detail="Not Found", exception=exc)
    else:
        return BoltError(status=500, kind="internal_error", detail="Internal Server Error", exception=exc)
    return BoltError(status=422, kind="validation", detail=_VALIDATION_DETAIL, errors=errors, exception=exc)


def _response_parts(response: Any, error: BoltError) -> tuple[int, list[tuple[str, str]], bytes]:
    """Convert the result of a formatter to (status, headers, body).

    The headers of the error follow, unless the response sets the same header.
    """
    if isinstance(response, JSON):
        content_type, body = "application/json", response.to_bytes()
    elif isinstance(response, PlainText):
        content_type, body = "text/plain; charset=utf-8", response.to_bytes()
    elif isinstance(response, HTML):
        content_type, body = "text/html; charset=utf-8", response.to_bytes()
    elif isinstance(response, Response):
        content_type, body = response.media_type, _render_response_body(response.content, response.media_type)
    else:
        raise TypeError(f"error_formatter must return Response, JSON, PlainText or HTML, not {type(response).__name__}")
    if getattr(response, "_cookies", None):
        raise TypeError("error_formatter cannot set cookies on an error response")
    status = response.status_code
    # A status out of this range can have no body, or it is not an error. An IntEnum such as HTTPStatus is an int.
    if not isinstance(status, int) or isinstance(status, bool) or not 400 <= status <= 599:
        raise TypeError(f"error_formatter must return a status from 400 to 599, not {status!r}")

    headers = [("content-type", content_type)]
    names = {"content-type"}
    for name, value in response.headers.items():
        if name.lower() == "content-type":
            headers[0] = ("content-type", value)
        else:
            headers.append((name, value))
            names.add(name.lower())
    for name, value in error.headers.items():
        if name.lower() not in names:
            headers.append((name, value))
    for name, value in headers:
        _check_header(name, value)
    return int(status), headers, body


def _check_header(name: Any, value: Any) -> None:
    """Raise TypeError for a header that is not valid in an HTTP response."""
    if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
        raise TypeError(f"error_formatter returned a header name that is not valid: {name!r}")
    if not isinstance(value, str) or _HEADER_VALUE_FORBIDDEN.search(value):
        raise TypeError(f"error_formatter returned a value that is not valid for the header {name!r}: {value!r}")


def render_error(
    formatter: ErrorFormatter, error: BoltError, request: Any
) -> tuple[int, list[tuple[str, str]], bytes] | None:
    """Call the formatter and convert its result to (status, headers, body).

    A formatter that fails is logged, and the result is None. Then the caller
    sends the default error response.
    """
    try:
        return _response_parts(formatter(error, request), error)
    except Exception:
        logger.exception(
            "error_formatter failed for a %d %s error. Bolt sends the default response.", error.status, error.kind
        )
        return None


def _log_formatted_error(error: BoltError) -> None:
    """Log a server error or a SuspiciousOperation as ``handle_exception`` logs it.

    It does not log an HTTPException.
    """
    if error.kind == "response_validation":
        _log_response_validation_error(error.exception, error.errors)
    elif DjangoSuspiciousOperation is not None and isinstance(error.exception, DjangoSuspiciousOperation):
        _log_suspicious_operation(error.exception)
    elif (
        error.kind == "internal_error"
        and error.exception is not None
        and not isinstance(error.exception, HTTPException)
    ):
        _log_server_error(error.exception)


def handle_exception_with_formatter(
    formatter: ErrorFormatter | None,
    exc: Exception,
    request: Any,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Handle an exception with the formatter, or with ``handle_exception`` when there is none.

    An HTTPException with a typed body skips the formatter. The handler chose that body.
    """
    if isinstance(exc, HTTPException) and exc.body is not None:
        return http_exception_handler(exc)
    if formatter is not None:
        error = bolt_error_from_exception(exc)
        parts = render_error(formatter, error, request)
        if parts is not None:
            _log_formatted_error(error)
            return parts
    return handle_exception(exc, debug=None, request=request)


def render_core_error(
    formatter: ErrorFormatter,
    status: int,
    kind: str,
    detail: str,
    errors_json: bytes | None,
    extra_json: bytes | None,
    headers: list[tuple[str, str]],
    request: Any,
    exception: BaseException | None,
    reason: str | None,
) -> tuple[int, list[tuple[str, str]], bytes] | None:
    """Format an error response of the Rust core. Rust calls this only on an error.

    ``exception`` is set when a Python exception reached Rust. Then the error
    comes from the exception, as for an error in ``_dispatch``. ``reason`` tells
    the cause of a Rust 500 for the log. The formatter does not get it. The
    result is None when the formatter fails, or for an HTTPException with a
    typed body. Then Rust sends its default response.
    """
    if exception is not None:
        if isinstance(exception, HTTPException) and exception.body is not None:
            return None
        error = bolt_error_from_exception(exception)
    else:
        error = BoltError(
            status=status,
            kind=kind,
            detail=detail,
            errors=_json.decode(errors_json) if errors_json is not None else [],
            headers=dict(headers),
            extra=_json.decode(extra_json) if extra_json is not None else None,
        )
    parts = render_error(formatter, error, request)
    if parts is not None:
        _log_formatted_error(error)
        if reason is not None:
            # The default body told the reason, and the formatter replaced it.
            logger.error("Internal Server Error: %s", reason)
    return parts
