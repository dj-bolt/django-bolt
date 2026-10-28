import re
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any


class BoltException(Exception):
    """Base exception class for all Django-Bolt exceptions."""

    detail: str

    def __init__(self, *args: Any, detail: str = "") -> None:
        """Initialize BoltException.

        Args:
            *args: Additional arguments
            detail: Exception detail message
        """
        str_args = [str(arg) for arg in args if arg]
        if not detail:
            if str_args:
                detail, *str_args = str_args
            elif hasattr(self, "detail"):
                detail = self.detail
        self.detail = detail
        super().__init__(*str_args)

    def __repr__(self) -> str:
        if self.detail:
            return f"{self.__class__.__name__} - {self.detail}"
        return self.__class__.__name__

    def __str__(self) -> str:
        return " ".join((*self.args, self.detail)).strip()


class HTTPException(BoltException):
    """Base exception for HTTP error responses.

    These exceptions carry information to construct an HTTP response.
    """

    status_code: int = 500
    """HTTP status code for this exception."""
    detail: str = ""
    """Exception details or message."""
    headers: dict[str, str]
    """Headers to attach to the response."""
    extra: dict[str, Any] | list[Any] | None = None
    """Additional data to include in the response."""
    body: Any = None
    """Typed response body. When set, it replaces the ``detail`` envelope."""

    def __init__(
        self,
        status_code: int | None = None,
        detail: Any | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | list[Any] | None = None,
        *,
        body: Any = None,
    ):
        """Initialize HTTPException.

        Args:
            status_code: HTTP status code (defaults to class status_code)
            detail: Exception details or message
            headers: HTTP headers to include in response
            extra: Additional data to include in response
            body: Typed body (for example a msgspec Struct). Encoded as-is,
                not validated. Declare its schema with ``response_model={code: Type}``.
        """
        # Handle detail
        if detail is None:
            detail = ""
        detail_str = str(detail) if detail else ""

        super().__init__(detail=detail_str)

        self.status_code = status_code if status_code is not None else self.status_code
        self.detail = detail_str if detail_str else HTTPStatus(self.status_code).phrase
        self.headers = headers or {}
        self.extra = extra
        self.body = body

        # Update args for better error messages
        self.args = (f"{self.status_code}: {self.detail}",)

    def __repr__(self) -> str:
        return f"{self.status_code} - {self.__class__.__name__} - {self.detail}"


class ValidationException(BoltException, ValueError):
    """Base exception for validation errors."""

    def __init__(self, errors: Sequence[Any]) -> None:
        """Initialize ValidationException.

        Args:
            errors: Sequence of validation errors
        """
        self._errors = errors
        super().__init__(detail="Validation error")

    def errors(self) -> Sequence[Any]:
        """Return validation errors."""
        return self._errors


class SerializationError(BoltException, ValueError):
    """Server-side serialization misuse or unsupported serializer state."""


class UnloadedRelationError(SerializationError):
    """A sync serializer path needs an unloaded Django relation."""


class RequestValidationError(ValidationException):
    """Request data validation error.

    Raised when request data (body, query params, headers, etc.) fails validation.
    """

    def __init__(self, errors: Sequence[Any], *, body: Any = None) -> None:
        """Initialize RequestValidationError.

        Args:
            errors: Sequence of validation errors
            body: The request body that failed validation
        """
        super().__init__(errors)
        self.body = body

    def __str__(self) -> str:
        """Return string representation with all error messages."""
        messages = []
        for err in self._errors:
            if isinstance(err, dict):
                loc = ".".join(str(x) for x in err.get("loc", []))
                msg = err.get("msg", "")
                if loc:
                    messages.append(f"{loc}: {msg}")
                else:
                    messages.append(msg)
            else:
                messages.append(str(err))
        return "; ".join(messages) if messages else "Validation error"


class ResponseValidationError(ValidationException):
    """Response data validation error.

    Raised when handler return value fails validation against response_model.
    """

    def __init__(self, errors: Sequence[Any], *, body: Any = None) -> None:
        """Initialize ResponseValidationError.

        Args:
            errors: Sequence of validation errors
            body: The response body that failed validation
        """
        super().__init__(errors)
        self.body = body

    def __str__(self) -> str:
        message = f"{len(self._errors)} validation error(s):\n"
        for err in self._errors:
            message += f"  {err}\n"
        return message


# HTTP 4xx Client Error Exceptions


class ClientException(HTTPException):
    """Base class for 4xx client errors."""

    status_code: int = 400


class BadRequest(ClientException):
    """400 Bad Request - Invalid request data."""

    status_code = 400


class Unauthorized(ClientException):
    """401 Unauthorized - Authentication required or failed."""

    status_code = 401


class Forbidden(ClientException):
    """403 Forbidden - Insufficient permissions."""

    status_code = 403


class NotFound(ClientException):
    """404 Not Found - Resource not found."""

    status_code = 404


class MethodNotAllowed(ClientException):
    """405 Method Not Allowed - HTTP method not supported for this endpoint."""

    status_code = 405


class NotAcceptable(ClientException):
    """406 Not Acceptable - Cannot produce response in requested format."""

    status_code = 406


class Conflict(ClientException):
    """409 Conflict - Request conflicts with current state."""

    status_code = 409


class Gone(ClientException):
    """410 Gone - Resource permanently deleted."""

    status_code = 410


class UnprocessableEntity(ClientException):
    """422 Unprocessable Entity - Semantic validation error."""

    status_code = 422


class TooManyRequests(ClientException):
    """429 Too Many Requests - Rate limit exceeded."""

    status_code = 429


# HTTP 5xx Server Error Exceptions


class ServerException(HTTPException):
    """Base class for 5xx server errors."""

    status_code: int = 500


class InternalServerError(ServerException):
    """500 Internal Server Error - Unexpected server error."""

    status_code = 500


class NotImplemented(ServerException):
    """501 Not Implemented - Endpoint not implemented."""

    status_code = 501


class BadGateway(ServerException):
    """502 Bad Gateway - Invalid response from upstream server."""

    status_code = 502


class ServiceUnavailable(ServerException):
    """503 Service Unavailable - Server temporarily unavailable."""

    status_code = 503


class GatewayTimeout(ServerException):
    """504 Gateway Timeout - Upstream server timeout."""

    status_code = 504


# Helper functions for better error messages


def parse_msgspec_decode_error(error: Exception, body_bytes: bytes) -> dict[str, Any]:
    """Parse msgspec.DecodeError to extract line/column information.

    Args:
        error: The msgspec.DecodeError exception
        body_bytes: The JSON bytes that failed to parse

    Returns:
        Dict with error details including line/column information
    """
    error_msg = str(error)

    # Try to extract byte position from error message
    # Format: "JSON is malformed: invalid character (byte 78)"
    match = re.search(r"byte (\d+)", error_msg)

    if match:
        byte_pos = int(match.group(1))

        # Calculate line and column from byte position
        lines = body_bytes.split(b"\n")
        current_pos = 0
        line_num = 1
        col_num = 0
        error_line_content = ""

        for i, line in enumerate(lines, 1):
            line_len = len(line) + 1  # +1 for newline
            if current_pos + line_len > byte_pos:
                line_num = i
                col_num = byte_pos - current_pos + 1  # +1 for human-readable column (1-indexed)
                error_line_content = line.decode("utf-8", errors="replace")
                break
            current_pos += line_len

        # Build descriptive error message
        msg = f"Invalid JSON at line {line_num}, column {col_num}: {error_msg}"
        if error_line_content:
            msg += f"\n  {error_line_content}\n  {' ' * (col_num - 1)}^"

        return {
            "type": "json_invalid",
            "loc": ("body", line_num, col_num),
            "msg": msg,
            "input": body_bytes.decode("utf-8", errors="replace")[:200] if body_bytes else "",
            "ctx": {
                "line": line_num,
                "column": col_num,
                "byte_position": byte_pos,
                "error": error_msg,
            },
        }

    # Fallback if we can't parse the byte position
    return {
        "type": "json_invalid",
        "loc": ("body",),
        "msg": error_msg,
        "input": body_bytes.decode("utf-8", errors="replace")[:100] if body_bytes else "",
    }
