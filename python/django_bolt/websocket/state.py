"""WebSocket connection state."""

from __future__ import annotations

from enum import IntEnum, auto


class WebSocketState(IntEnum):
    """Connection state of a WebSocket.

    The state only moves forward::

        CONNECTING --accept()--> CONNECTED --close() or client leaves--> DISCONNECTED
        CONNECTING --close()--> DISCONNECTED  (the server refuses the handshake with 403)

    A method that the state does not allow raises ``RuntimeError``. The message
    names the state and the method:

    - ``send()`` before ``accept()``, and ``receive_text()``, ``receive_bytes()``
      or ``receive_json()`` before ``accept()``.
    - ``send()`` or ``accept()`` after the connection closed.

    ``receive()`` after the connection closed raises ``WebSocketDisconnect``.
    A second ``accept()`` and a second ``close()`` do nothing.
    """

    CONNECTING = auto()
    CONNECTED = auto()
    DISCONNECTED = auto()
