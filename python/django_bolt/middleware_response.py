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
Response = tuple[int, ResponseMetaTuple, str, ResponseBody]


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
        body_kind: str = "bytes",
    ):
        self.status_code = status_code
        self.headers = headers  # Dict for easy middleware modification
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
        if not (isinstance(meta, tuple) and len(meta) == 4):
            raise TypeError("Invalid middleware response metadata tuple")
        if body_kind not in {"bytes", "stream", "file"}:
            raise TypeError(f"Invalid middleware response body_kind {body_kind!r}")

        response_type, custom_ct, custom_headers, cookies = meta
        headers: dict[str, str] = {}

        if custom_ct:
            headers["content-type"] = custom_ct
        if custom_headers:
            for k, v in custom_headers:
                headers[k.lower()] = v

        return cls(status_code, headers, body, response_type, cookies, body_kind=body_kind)

    def to_tuple(self) -> Response:
        """Convert back to internal tuple format.

        Returns ResponseMeta format so Rust handles all header/cookie serialization.
        """
        # Extract content-type if middleware set it
        custom_ct = self.headers.pop("content-type", None)

        # Build custom headers list (excluding content-type which is handled separately)
        custom_headers: list[tuple[str, str]] | None = None
        if self.headers:
            custom_headers = [(k, v) for k, v in self.headers.items()]

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
            body_kind = "stream"

        body_payload = bytes(self.body) if isinstance(self.body, bytearray) else self.body
        return (self.status_code, meta, body_kind, body_payload)
