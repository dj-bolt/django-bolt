"""WebSocket-specific exceptions."""

from __future__ import annotations

from .close_codes import CloseCode


class WebSocketException(Exception):
    """Base exception for WebSocket errors.

    Raise it in a handler to close the connection with ``code`` and ``reason``.
    Before ``accept()``, it refuses the handshake with 403.
    """

    def __init__(self, code: int = CloseCode.INTERNAL_ERROR, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"WebSocket error {code}: {reason}")


class WebSocketDisconnect(WebSocketException):
    """Raised when a WebSocket connection is closed."""

    def __init__(self, code: int = CloseCode.NORMAL, reason: str = "") -> None:
        super().__init__(code, reason)


class WebSocketClose(WebSocketException):
    """Raise it in a handler to close the connection. The default code is 1000."""

    def __init__(self, code: int = CloseCode.NORMAL, reason: str = "") -> None:
        super().__init__(code, reason)
