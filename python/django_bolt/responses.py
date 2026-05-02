from __future__ import annotations

import inspect
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import msgspec

# Django import - may fail if Django not configured, kept at top for consistency
try:
    from django.conf import settings as django_settings
except ImportError:
    django_settings = None

from . import _json
from .cookies import Cookie, make_delete_cookie

if TYPE_CHECKING:
    from .cookies import SameSitePolicy

T = TypeVar("T", bound="CookieMixin")


class CookieMixin:
    """Mixin providing set_cookie() and delete_cookie() methods for response classes."""

    _cookies: list[Cookie]

    def set_cookie(
        self: T,
        name: str,
        value: str = "",
        max_age: int | None = None,
        expires: datetime | str | None = None,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: SameSitePolicy = "Lax",
    ) -> T:
        """Set a cookie on the response.

        Matches Django's HttpResponse.set_cookie() API for familiarity.

        Args:
            name: Cookie name
            value: Cookie value (default: "")
            max_age: Maximum age in seconds (default: None, session cookie)
            expires: Expiration datetime or string (default: None)
            path: Cookie path (default: "/")
            domain: Cookie domain (default: None, current domain)
            secure: Require HTTPS (default: False)
            httponly: Prevent JavaScript access (default: False)
            samesite: SameSite policy - "Strict", "Lax", "None", or False (default: "Lax")

        Returns:
            Self for method chaining

        Example:
            return Response({"ok": True}).set_cookie("session", "abc123", httponly=True)
        """
        if not hasattr(self, "_cookies"):
            self._cookies = []
        self._cookies.append(Cookie(name, value, max_age, expires, path, domain, secure, httponly, samesite))
        return self

    def delete_cookie(self: T, name: str, path: str = "/", domain: str | None = None) -> T:
        """Delete a cookie by setting it to expire immediately.

        Args:
            name: Cookie name to delete
            path: Cookie path (default: "/")
            domain: Cookie domain (default: None)

        Returns:
            Self for method chaining

        Example:
            return Response({"ok": True}).delete_cookie("old_session")
        """
        if not hasattr(self, "_cookies"):
            self._cookies = []
        self._cookies.append(make_delete_cookie(name, path, domain))
        return self


# Cache for BOLT_ALLOWED_FILE_PATHS - loaded once at server startup
_ALLOWED_FILE_PATHS_CACHE: list[Path] | None = None
_ALLOWED_FILE_PATHS_INITIALIZED = False


def initialize_file_response_settings():
    """
    Initialize FileResponse settings cache at server startup.
    This should be called once when the server starts to cache BOLT_ALLOWED_FILE_PATHS.
    """
    global _ALLOWED_FILE_PATHS_CACHE, _ALLOWED_FILE_PATHS_INITIALIZED

    if _ALLOWED_FILE_PATHS_INITIALIZED:
        return

    try:
        if django_settings and hasattr(django_settings, "BOLT_ALLOWED_FILE_PATHS"):
            allowed_paths = django_settings.BOLT_ALLOWED_FILE_PATHS
            # Resolve all paths once at startup
            _ALLOWED_FILE_PATHS_CACHE = [Path(p).resolve() for p in allowed_paths] if allowed_paths else None
        else:
            _ALLOWED_FILE_PATHS_CACHE = None
    except ImportError:
        # Django not configured, allow any path (development mode)
        _ALLOWED_FILE_PATHS_CACHE = None

    _ALLOWED_FILE_PATHS_INITIALIZED = True


class Response(CookieMixin):
    """
    Generic HTTP response with custom headers.

    Use this when you need to return a response with custom headers (like Allow for OPTIONS).

    Examples:
        # OPTIONS handler with Allow header
        @api.options("/items")
        async def options_items():
            return Response({}, headers={"Allow": "GET, POST, PUT, DELETE"})

        # Custom response with additional headers
        @api.get("/data")
        async def get_data():
            return Response(
                {"result": "data"},
                status_code=200,
                headers={"X-Custom-Header": "value"}
            )

        # Response with cookies
        @api.post("/login")
        async def login():
            return Response({"ok": True}).set_cookie("session", "abc123", httponly=True)
    """

    def __init__(
        self,
        content: Any = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        media_type: str = "application/json",
    ):
        self.content = content if content is not None else {}
        self.status_code = status_code
        self.headers = headers or {}
        self.media_type = media_type

    def to_bytes(self) -> bytes:
        if self.media_type == "application/json":
            return _json.encode(self.content)
        elif isinstance(self.content, str):
            return self.content.encode()
        elif isinstance(self.content, bytes):
            return self.content
        else:
            return str(self.content).encode()


class JSON(CookieMixin):
    def __init__(self, data: Any, status_code: int = 200, headers: dict[str, str] | None = None):
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}

    def to_bytes(self) -> bytes:
        return _json.encode(self.data)


class PlainText(CookieMixin):
    def __init__(self, text: str, status_code: int = 200, headers: dict[str, str] | None = None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def to_bytes(self) -> bytes:
        return self.text.encode()


class HTML(CookieMixin):
    def __init__(self, html: str, status_code: int = 200, headers: dict[str, str] | None = None):
        self.html = html
        self.status_code = status_code
        self.headers = headers or {}

    def to_bytes(self) -> bytes:
        return self.html.encode()


class Redirect(CookieMixin):
    def __init__(self, url: str, status_code: int = 307, headers: dict[str, str] | None = None):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}


class File(CookieMixin):
    def __init__(
        self,
        path: str,
        *,
        media_type: str | None = None,
        filename: str | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self.path = path
        self.media_type = media_type
        self.filename = filename
        self.status_code = status_code
        self.headers = headers or {}

    def read_bytes(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()


class UploadFile:
    def __init__(self, name: str, filename: str | None, content_type: str | None, path: str):
        self.name = name
        self.filename = filename
        self.content_type = content_type
        self.path = path

    def read(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()


class FileResponse(CookieMixin):
    def __init__(
        self,
        path: str,
        *,
        media_type: str | None = None,
        filename: str | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        # SECURITY: Validate and canonicalize path to prevent traversal

        # Convert to absolute path and resolve any .. or symlinks
        try:
            resolved_path = Path(path).resolve()
        except (OSError, RuntimeError) as e:
            raise ValueError(f"Invalid file path: {e}") from e

        # Check if the file exists and is a regular file (not a directory or special file)
        if not resolved_path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        if not resolved_path.is_file():
            raise ValueError(f"Path is not a regular file: {path}")

        # Check against allowed directories if configured (using cached value)
        if _ALLOWED_FILE_PATHS_CACHE is not None:
            # Ensure the resolved path is within one of the allowed directories
            is_allowed = False
            for allowed_path in _ALLOWED_FILE_PATHS_CACHE:
                try:
                    # Check if resolved_path is relative to allowed_path
                    resolved_path.relative_to(allowed_path)
                    is_allowed = True
                    break
                except ValueError:
                    # Not a subpath, continue checking
                    continue

            if not is_allowed:
                raise PermissionError(
                    f"File path '{path}' is not within allowed directories. "
                    f"Configure BOLT_ALLOWED_FILE_PATHS in Django settings."
                )

        self.path = str(resolved_path)
        self.media_type = media_type
        self.filename = filename
        self.status_code = status_code
        self.headers = headers or {}


class StreamingResponse(CookieMixin):
    def __init__(
        self,
        content: Any,
        *,
        status_code: int = 200,
        media_type: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        # Validate that content is already a called generator/iterator, not a callable
        if callable(content):
            if inspect.isasyncgenfunction(content) or inspect.isgeneratorfunction(content):
                raise TypeError(
                    "StreamingResponse requires a generator instance, not a generator function. "
                    "Call your generator function with parentheses: StreamingResponse(gen(), ...) "
                    "not StreamingResponse(gen, ...)"
                )
            # If it's some other callable (not a generator function), raise an error
            raise TypeError(
                f"StreamingResponse content must be a generator instance (e.g., gen() or agen()), "
                f"not a callable. Received: {type(content).__name__}"
            )

        self.content = content
        self.status_code = status_code
        self.media_type = media_type or "application/octet-stream"
        self.headers = headers or {}

        # Detect generator type at instantiation time (once per request, not per chunk)
        # This avoids repeated Python inspect calls in Rust streaming loop
        self.is_async_generator = False

        if hasattr(content, "__aiter__") or hasattr(content, "__anext__"):
            # Async generator instance
            self.is_async_generator = True
        elif not (hasattr(content, "__iter__") or hasattr(content, "__next__")):
            # Not a generator/iterator
            raise TypeError(
                f"StreamingResponse content must be a generator instance. Received type: {type(content).__name__}"
            )


class ServerSentEvent(msgspec.Struct, frozen=True):
    """Represents a single Server-Sent Event.

    When yielded from a handler using ``response_class=EventSourceResponse``,
    each ``ServerSentEvent`` is encoded into the SSE wire format
    (``text/event-stream``).

    Yielding a plain object (dict, Pydantic/msgspec model, etc.) instead
    auto-JSON-encodes it as the ``data:`` field.

    Examples::

        yield ServerSentEvent(data={"price": 42.5}, event="update", id="1")
        yield ServerSentEvent(raw_data="plain text line", comment="keepalive")
        yield ServerSentEvent(retry=5000)
    """

    data: Any = None
    raw_data: str | None = None
    event: str | None = None
    id: str | None = None
    retry: int | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        if self.data is not None and self.raw_data is not None:
            raise ValueError(
                "Cannot set both 'data' and 'raw_data' on the same "
                "ServerSentEvent. Use 'data' for JSON-serialized payloads "
                "or 'raw_data' for pre-formatted strings."
            )
        if self.id is not None and "\0" in self.id:
            raise ValueError("SSE 'id' must not contain null characters")
        if self.retry is not None and self.retry < 0:
            raise ValueError("SSE 'retry' must be a non-negative integer")


def format_sse_event(
    *,
    data_str: str | None = None,
    data_bytes: bytes | None = None,
    event: str | None = None,
    id: str | None = None,
    retry: int | None = None,
    comment: str | None = None,
) -> bytes:
    """Build SSE wire-format bytes from pre-serialized data.

    The result always ends with ``\\n\\n`` (the event terminator).

    Pass ``data_bytes`` instead of ``data_str`` when the payload is already
    encoded (e.g. from msgspec) to avoid a decode/encode round-trip.
    """
    # Fast path: data-only event with pre-encoded bytes (most common from auto-framing)
    if data_bytes is not None and event is None and id is None and retry is None and comment is None:
        return b"data: " + data_bytes + b"\n\n"

    lines: list[str] = []

    if comment is not None:
        for line in comment.splitlines():
            lines.append(f": {line}")

    if event is not None:
        lines.append(f"event: {event}")

    if data_str is not None:
        for line in data_str.splitlines():
            lines.append(f"data: {line}")
    elif data_bytes is not None:
        for line in data_bytes.decode("utf-8").splitlines():
            lines.append(f"data: {line}")

    if id is not None:
        lines.append(f"id: {id}")

    if retry is not None:
        lines.append(f"retry: {retry}")

    lines.append("")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


# Keep-alive comment per the SSE spec recommendation
SSE_KEEPALIVE_COMMENT = b": ping\n\n"

# Default seconds between keep-alive pings when the generator is idle
SSE_DEFAULT_PING_INTERVAL: float = 15.0


class EventSourceResponse(StreamingResponse):
    """Streaming response with ``text/event-stream`` media type.

    Use as ``response_class=EventSourceResponse`` on a route decorator to enable
    Server-Sent Events with automatic SSE framing, compression skipping, and
    keep-alive pings.

    Works with **any HTTP method** (GET, POST, etc.), which makes it compatible
    with protocols like MCP that stream SSE over POST.

    **Implicit (preferred)** — the handler itself is a generator::

        @api.get("/items/stream", response_class=EventSourceResponse)
        async def stream_items() -> AsyncIterable[Item]:
            for item in items:
                yield item

    **Explicit** — full control over the response::

        @api.get("/stream")
        async def stream():
            async def gen():
                yield {"message": "hello"}
                yield ServerSentEvent(data=item, event="update", id="1")
            return EventSourceResponse(gen())
    """

    def __init__(
        self,
        content: Any,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        ping_interval: float | None = SSE_DEFAULT_PING_INTERVAL,
    ):
        super().__init__(
            content,
            status_code=status_code,
            media_type="text/event-stream",
            headers=headers,
        )
        self.ping_interval = ping_interval
