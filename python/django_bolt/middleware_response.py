"""
MiddlewareResponse class for middleware compatibility.

This is in a separate module to avoid circular imports:
- api.py imports from middleware
- middleware imports from django_adapter
- django_adapter needs MiddlewareResponse
"""

from __future__ import annotations

from typing import Any

from .responses import StreamingResponse
from .serialization import (
    _BODY_BYTES,
    _BODY_FILE,
    _BODY_STREAM,
    _RESPONSE_META_EMPTY,
    _RESPONSE_META_HTML,
    _RESPONSE_META_JSON,
    _RESPONSE_META_OCTETSTREAM,
    _RESPONSE_META_PLAINTEXT,
)

# Map integer meta tags back to (response_type, None, None, None) for middleware compat
_META_TAG_TO_RESPONSE_TYPE = {
    _RESPONSE_META_JSON: "json",
    _RESPONSE_META_PLAINTEXT: "plaintext",
    _RESPONSE_META_OCTETSTREAM: "octetstream",
    _RESPONSE_META_EMPTY: "empty",
    _RESPONSE_META_HTML: "html",
}

# The Content-Type of each built-in response type, as Rust sends it
# (ResponseType in crates/bolt-core/src/response_meta.rs).
_RESPONSE_TYPE_CONTENT_TYPES = {
    "json": "application/json",
    "html": "text/html; charset=utf-8",
    "plaintext": "text/plain; charset=utf-8",
    "octetstream": "application/octet-stream",
}
# Rust sends no Content-Type for these types.
_RESPONSE_TYPES_WITHOUT_CONTENT_TYPE = frozenset(("empty", "redirect", "file", "streaming"))


def _response_type_content_type(response_type: str) -> str | None:
    """Return the Content-Type that Rust sends for a response type, or None.

    Rust reads a response type that it does not know as octetstream.
    """
    if response_type in _RESPONSE_TYPES_WITHOUT_CONTENT_TYPE:
        return None
    return _RESPONSE_TYPE_CONTENT_TYPES.get(response_type, "application/octet-stream")


# Raw cookie tuple type (matches serialization.py CookieTuple)
CookieTuple = tuple[str, str, str, int | None, str | None, str | None, bool, bool, str | None]

# ResponseMeta tuple type for Rust-side header building
ResponseMetaTuple = tuple[
    str,  # response_type
    str | None,  # custom_content_type
    list[tuple[str, str]] | None,  # custom_headers
    list[CookieTuple] | None,  # cookies (raw tuples, NOT serialized)
]

ResponseBody = bytes | StreamingResponse | str
Response = tuple[int, ResponseMetaTuple, int, ResponseBody]


class ResponseHeaders(dict[str, str]):
    """Response headers whose names ignore letter case.

    Each name is stored in lower case. Thus ``headers["Content-Type"] = ...``
    replaces the value that ``headers["content-type"]`` holds, and either name
    reads it.
    """

    __slots__ = ()

    def __init__(self, headers: Any = (), /) -> None:
        items = headers.items() if hasattr(headers, "items") else headers
        super().__init__({name.lower(): value for name, value in items})

    def __setitem__(self, name: str, value: str) -> None:
        super().__setitem__(name.lower(), value)

    def __getitem__(self, name: str) -> str:
        return super().__getitem__(name.lower())

    def __delitem__(self, name: str) -> None:
        super().__delitem__(name.lower())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and super().__contains__(name.lower())

    def __ior__(self, headers: Any) -> ResponseHeaders:  # type: ignore[override]
        self.update(headers)
        return self

    def get(self, name: str, default: Any = None) -> Any:  # type: ignore[override]
        return super().get(name.lower(), default)

    def pop(self, name: str, *default: Any) -> Any:  # type: ignore[override]
        return super().pop(name.lower(), *default)

    def setdefault(self, name: str, default: str) -> str:  # type: ignore[override]
        return super().setdefault(name.lower(), default)

    def update(self, headers: Any = (), /, **more: str) -> None:  # type: ignore[override]
        items = headers.items() if hasattr(headers, "items") else headers
        for name, value in items:
            self[name] = value
        for name, value in more.items():
            self[name] = value

    @classmethod
    def _from_lower(cls, headers: dict[str, str]) -> ResponseHeaders:
        """Copy headers whose names are already in lower case, with no Python loop."""
        result = cls.__new__(cls)
        dict.update(result, headers)
        return result


# The response type and the headers of each integer meta tag. The tag carries the
# Content-Type, and middleware reads it from the headers.
_META_TAG_HEADERS = {
    tag: (response_type, {} if (ct := _response_type_content_type(response_type)) is None else {"content-type": ct})
    for tag, response_type in _META_TAG_TO_RESPONSE_TYPE.items()
}


def _split_content_type_headers(
    headers: dict[str, str] | None,
) -> tuple[str | None, list[tuple[str, str]] | None]:
    """Split a headers dict into content-type and remaining headers.

    Header names are treated case-insensitively. A plain dict can hold two
    content-type keys with other letter cases; the last one wins.
    """
    if not headers:
        return None, None

    content_type = None
    remaining_headers: list[tuple[str, str]] = []

    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = value
            continue
        remaining_headers.append((key, value))

    return content_type, remaining_headers or None


class MiddlewareResponse:
    """
    Response wrapper for middleware compatibility.

    Middleware expects response.status_code and response.headers attributes,
    but our internal response format is a tuple (status_code, headers/meta, body).
    This class bridges the gap, allowing middleware to modify responses.

    IMPORTANT: Cookie serialization happens in Rust, not Python.
    This class stores raw cookie tuples and returns ResponseMeta format
    so Rust can handle all header/cookie serialization.
    """

    __slots__ = ("status_code", "headers", "body", "_response_type", "_raw_cookies", "_body_kind")

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: bytes | StreamingResponse | str,
        response_type: str = "json",
        raw_cookies: list[CookieTuple] | None = None,
        body_kind: int = _BODY_BYTES,
    ):
        self.status_code = status_code
        # Middleware reads and changes these. A name ignores letter case.
        headers = headers if type(headers) is ResponseHeaders else ResponseHeaders(headers)
        if not dict.__contains__(headers, "content-type"):
            # The headers hold the Content-Type of the response type, so a
            # Content-Type that is not there was deleted.
            content_type = _response_type_content_type(response_type)
            if content_type is not None:
                dict.__setitem__(headers, "content-type", content_type)
        self.headers = headers
        self.body = body
        self._response_type = response_type  # Preserve for Rust content-type
        self._raw_cookies = raw_cookies or []  # Raw tuples, Rust serializes
        self._body_kind = body_kind

    @classmethod
    def from_tuple(cls, response: Any) -> MiddlewareResponse:
        """Create from internal tuple format.

        Expects ResponseWireV1: (status, meta_tuple, body_kind, body_payload).
        IMPORTANT: Does NOT serialize cookies - preserves raw tuples for Rust.
        """
        if not (isinstance(response, tuple) and len(response) == 4):
            raise TypeError("Middleware response must be a ResponseWireV1 4-tuple.")

        status_code, meta, body_kind, body = response
        if body_kind not in {_BODY_BYTES, _BODY_STREAM, _BODY_FILE}:
            raise TypeError(f"Invalid middleware response body_kind {body_kind!r}")

        # Handle integer meta tags (fast path) and tuple meta (slow path)
        if isinstance(meta, int):
            tag = _META_TAG_HEADERS.get(meta)
            if tag is None:
                raise TypeError(f"Unknown response meta tag {meta!r}")
            response_type, tag_headers = tag
            headers = ResponseHeaders._from_lower(tag_headers)
            return cls(status_code, headers, body, response_type, None, body_kind=body_kind)

        if not (isinstance(meta, tuple) and len(meta) == 4):
            raise TypeError("Invalid middleware response metadata tuple")

        response_type, custom_ct, custom_headers, cookies = meta
        headers = ResponseHeaders(custom_headers or ())
        # The custom content type, else the one of the response type. An empty
        # custom content type means none. A content-type in the custom headers
        # wins, as before.
        content_type = custom_ct if custom_ct is not None else _response_type_content_type(response_type)
        if content_type is not None:
            headers.setdefault("content-type", content_type)

        return cls(status_code, headers, body, response_type, cookies, body_kind=body_kind)

    def to_tuple(self) -> Response:
        """Convert back to internal tuple format.

        Returns ResponseMeta format so Rust handles all header/cookie serialization.
        """
        headers = self.headers
        if type(headers) is ResponseHeaders:
            # Each name is in lower case, so one lookup finds the content type.
            custom_ct = dict.get(headers, "content-type")
            custom_headers = (
                [(name, value) for name, value in dict.items(headers) if name != "content-type"]
                if len(headers) > (custom_ct is not None)
                else None
            )
        else:
            custom_ct, custom_headers = _split_content_type_headers(headers)
        if custom_ct is None:
            # The headers have no Content-Type, so send none. Rust reads an empty
            # one as none; without it, Rust sends the one of the response type.
            custom_ct = ""

        # Return ResponseMeta format - Rust serializes headers and cookies
        meta: ResponseMetaTuple = (
            self._response_type,
            custom_ct,
            custom_headers,
            self._raw_cookies if self._raw_cookies else None,
        )

        # Trust the slot set at construction time; only override when middleware
        # has actually swapped in a StreamingResponse object.
        body_kind = self._body_kind
        if isinstance(self.body, StreamingResponse):
            body_kind = _BODY_STREAM

        body_payload = bytes(self.body) if isinstance(self.body, bytearray) else self.body
        return (self.status_code, meta, body_kind, body_payload)
