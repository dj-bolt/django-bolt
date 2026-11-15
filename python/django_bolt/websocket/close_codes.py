"""WebSocket close codes as defined in RFC 6455."""

from __future__ import annotations


class CloseCode:
    """Standard WebSocket close codes."""

    NORMAL = 1000
    GOING_AWAY = 1001
    PROTOCOL_ERROR = 1002
    UNSUPPORTED_DATA = 1003
    NO_STATUS_RECEIVED = 1005
    ABNORMAL_CLOSURE = 1006
    INVALID_FRAME_PAYLOAD_DATA = 1007
    POLICY_VIOLATION = 1008
    MESSAGE_TOO_BIG = 1009
    MANDATORY_EXTENSION = 1010
    INTERNAL_ERROR = 1011
    SERVICE_RESTART = 1012
    TRY_AGAIN_LATER = 1013
    BAD_GATEWAY = 1014
    TLS_HANDSHAKE = 1015


# RFC 6455 5.5: the payload of a control frame is at most 125 bytes. The close code takes 2.
MAX_CLOSE_REASON_BYTES = 123

# RFC 6455 7.4.1: an endpoint must not send these codes in a close frame.
_CODES_NOT_FOR_A_FRAME = frozenset({CloseCode.NO_STATUS_RECEIVED, CloseCode.ABNORMAL_CLOSURE, CloseCode.TLS_HANDSHAKE})


def check_close_frame(code: int, reason: str) -> None:
    """Raise ``ValueError`` for a code or a reason that makes an invalid close frame.

    The server does the same check on each ``websocket.close`` message.
    """
    if not isinstance(code, int) or not 1000 <= code <= 4999 or code in _CODES_NOT_FOR_A_FRAME:
        raise ValueError(f"WebSocket close code {code} is not valid: use 1000 to 4999, but not 1005, 1006 or 1015")
    size = len(reason.encode("utf-8"))
    if size > MAX_CLOSE_REASON_BYTES:
        raise ValueError(
            f"WebSocket close reason is {size} bytes: the limit is {MAX_CLOSE_REASON_BYTES} bytes of UTF-8"
        )


# Alias for convenience
WS_1000_NORMAL_CLOSURE = CloseCode.NORMAL
WS_1001_GOING_AWAY = CloseCode.GOING_AWAY
WS_1002_PROTOCOL_ERROR = CloseCode.PROTOCOL_ERROR
WS_1003_UNSUPPORTED_DATA = CloseCode.UNSUPPORTED_DATA
WS_1008_POLICY_VIOLATION = CloseCode.POLICY_VIOLATION
WS_1009_MESSAGE_TOO_BIG = CloseCode.MESSAGE_TOO_BIG
WS_1011_INTERNAL_ERROR = CloseCode.INTERNAL_ERROR
WS_1012_SERVICE_RESTART = CloseCode.SERVICE_RESTART
WS_1013_TRY_AGAIN_LATER = CloseCode.TRY_AGAIN_LATER
