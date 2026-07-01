"""MCP / JSON-RPC wire types and protocol constants.

These are plain data definitions (constants + msgspec structs) with no behavior.
"""

from __future__ import annotations

from typing import Any

import msgspec

# ── Protocol versions ────────────────────────────────────────────────────────
# Preferred version we advertise; SUPPORTED is the set we will accept/negotiate.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
)

# ── JSON-RPC error codes ─────────────────────────────────────────────────────
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

JSONRPC_VERSION = "2.0"


class Incoming(msgspec.Struct, omit_defaults=True):
    """Lenient inbound JSON-RPC envelope.

    A *request* has both ``method`` and ``id``; a *notification* has ``method``
    but no ``id``; a *response* (from the client) has ``id`` but no ``method``.
    ``id`` is kept as int|str and echoed back verbatim — never coerced.
    """

    jsonrpc: str = JSONRPC_VERSION
    id: int | str | None = None
    method: str | None = None
    params: dict[str, Any] | None = None
    result: Any = None
    error: Any = None


def is_request(msg: Incoming) -> bool:
    return msg.method is not None and msg.id is not None
