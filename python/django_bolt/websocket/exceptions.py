"""WebSocket-specific exceptions."""

from __future__ import annotations

from .close_codes import CloseCode, check_close_frame


class WebSocketException(Exception):
    """Base exception for WebSocket errors.

    Raise it in a handler to close the connection with ``code`` and ``reason``.
    Before ``accept()``, it refuses the handshake with 403.

    Raises:
        ValueError: The code or the reason makes an invalid close frame. The
            rules are those of ``WebSocket.close()``.
    """

    def __init__(self, code: int = CloseCode.INTERNAL_ERROR, reason: str = "") -> None:
        check_close_frame(code, reason)
        self.code = code
        self.reason = reason
        super().__init__(f"WebSocket error {code}: {reason}")


class WebSocketDisconnect(WebSocketException):
    """Raised when a WebSocket connection is closed.

    It reports the close of the client and asks for no close frame. Thus its
    code has no check: it can be a code that no close frame holds, such as 1006.
    """

    def __init__(self, code: int = CloseCode.NORMAL, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        Exception.__init__(self, f"WebSocket error {code}: {reason}")


class WebSocketClose(WebSocketException):
    """Raise it in a handler to close the connection. The default code is 1000."""

    def __init__(self, code: int = CloseCode.NORMAL, reason: str = "") -> None:
        super().__init__(code, reason)
