"""WebSocket connection type - FastAPI-like interface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import msgspec

from .close_codes import CloseCode
from .exceptions import WebSocketDisconnect
from .state import WebSocketState

if TYPE_CHECKING:
    from collections.abc import Mapping


class WebSocket:
    """
    WebSocket connection object with FastAPI-like interface.

    Usage:
        @api.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            while True:
                data = await websocket.receive_text()
                await websocket.send_text(f"Echo: {data}")
    """

    def __init__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._scope = scope
        self._receive = receive
        self._send = send
        self._state = WebSocketState.CONNECTING
        self._accepted = False

    @property
    def state(self) -> WebSocketState:
        """Current connection state."""
        return self._state

    @property
    def url(self) -> str:
        """The WebSocket URL."""
        return self._scope.get("path", "/")

    @property
    def path(self) -> str:
        """The URL path."""
        return self._scope.get("path", "/")

    @property
    def query_string(self) -> bytes:
        """Raw query string as bytes."""
        return self._scope.get("query_string", b"")

    @property
    def path_params(self) -> dict[str, str]:
        """Path parameters extracted from the URL."""
        return self._scope.get("path_params", {})

    @property
    def headers(self) -> Mapping[str, str]:
        """Request headers."""
        return self._scope.get("headers", {})

    @property
    def query_params(self) -> dict[str, str]:
        """Query parameters parsed from query string."""
        if not hasattr(self, "_query_params"):
            qs = self.query_string.decode("utf-8", errors="replace")
            parsed = parse_qs(qs, keep_blank_values=True)
            self._query_params = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
        return self._query_params

    @property
    def cookies(self) -> dict[str, str]:
        """Request cookies."""
        return self._scope.get("cookies", {})

    @property
    def subprotocols(self) -> list[str]:
        """Subprotocols that the client requested, in order of preference."""
        return self._scope.get("subprotocols", [])

    @property
    def client(self) -> tuple[str, int] | None:
        """Client address as (host, port) tuple."""
        return self._scope.get("client")

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        """
        Accept the WebSocket connection.

        Must be called before sending or receiving messages.

        Args:
            subprotocol: One of ``subprotocols``. The server sends it in the
                ``Sec-WebSocket-Protocol`` header of the 101 response.
            headers: Extra ``(name, value)`` byte pairs for the 101 response.
        """
        if self._accepted:
            return

        message: dict[str, Any] = {"type": "websocket.accept"}
        if subprotocol is not None:
            message["subprotocol"] = subprotocol
        if headers is not None:
            message["headers"] = headers

        await self._send(message)
        self._state = WebSocketState.CONNECTED
        self._accepted = True

    async def receive(self) -> dict[str, Any]:
        """
        Receive a raw WebSocket message.

        Returns a dict with 'type' key indicating message type.
        """
        if self._state == WebSocketState.DISCONNECTED:
            raise WebSocketDisconnect(CloseCode.NORMAL)

        message = await self._receive()
        message_type = message.get("type", "")

        if message_type == "websocket.disconnect":
            self._state = WebSocketState.DISCONNECTED
            code = message.get("code", CloseCode.NORMAL)
            raise WebSocketDisconnect(code=code)

        return message

    async def receive_text(self) -> str:
        """Receive a text message."""
        message = await self.receive()
        return message.get("text", "")

    async def receive_bytes(self) -> bytes:
        """Receive a binary message."""
        message = await self.receive()
        return message.get("bytes", b"")

    async def receive_json(self, mode: str = "text") -> Any:
        """
        Receive and parse a JSON message.

        Args:
            mode: 'text' or 'binary' - how the JSON was sent
        """
        if mode == "text":
            data = await self.receive_text()
        else:
            data = await self.receive_bytes()
            data = data.decode("utf-8")
        return msgspec.json.decode(data)

    async def send(self, message: dict[str, Any]) -> None:
        """Send a raw WebSocket message."""
        if self._state != WebSocketState.CONNECTED:
            raise RuntimeError("WebSocket is not connected")
        await self._send(message)

    async def send_text(self, data: str) -> None:
        """Send a text message."""
        await self.send({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        """Send a binary message."""
        await self.send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: Any, mode: str = "text") -> None:
        """
        Send data as JSON.

        Args:
            data: Data to serialize as JSON
            mode: 'text' or 'binary' - how to send the JSON
        """
        text = msgspec.json.encode(data).decode("utf-8")
        if mode == "text":
            await self.send_text(text)
        else:
            await self.send_bytes(text.encode("utf-8"))

    async def close(
        self,
        code: int = CloseCode.NORMAL,
        reason: str = "",
    ) -> None:
        """
        Close the WebSocket connection.

        Args:
            code: WebSocket close code (default: 1000 Normal Closure)
            reason: Human-readable close reason
        """
        if self._state == WebSocketState.DISCONNECTED:
            return

        await self._send(
            {
                "type": "websocket.close",
                "code": code,
                "reason": reason,
            }
        )
        self._state = WebSocketState.DISCONNECTED

    async def iter_text(self):
        """Async iterator for text messages."""
        try:
            while True:
                yield await self.receive_text()
        except WebSocketDisconnect:
            return

    async def iter_bytes(self):
        """Async iterator for binary messages."""
        try:
            while True:
                yield await self.receive_bytes()
        except WebSocketDisconnect:
            return

    async def iter_json(self):
        """Async iterator for JSON messages."""
        try:
            while True:
                yield await self.receive_json()
        except WebSocketDisconnect:
            return
