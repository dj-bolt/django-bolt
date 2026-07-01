"""Session management for the Streamable HTTP transport.

``SessionManager`` is the stateful, single-process registry (the GET SSE listen
channel reads from a per-session ``asyncio.Queue``). ``StatelessSessions`` is the
multi-process-safe no-op variant.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field

# Sentinel pushed onto a session queue to unblock and close the GET listen stream.
SESSION_CLOSE = object()


@dataclass
class Session:
    id: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    protocol_version: str | None = None
    get_stream_open: bool = False
    # Capabilities the client advertised at initialize (e.g. {"sampling": {}, "elicitation": {}}).
    client_capabilities: dict = field(default_factory=dict)
    # Server→client requests (sampling/elicitation) awaiting the client's response.
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    _request_counter: int = 0

    def next_request_id(self) -> int:
        self._request_counter += 1
        return self._request_counter


class SessionManager:
    """Stateful, in-process session registry."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        session = Session(id=secrets.token_hex(16))
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str | None) -> Session | None:
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    def terminate(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        # Cancel any server→client requests still awaiting a reply — the session is
        # gone, so the reply can never arrive; unblock the tools awaiting them.
        for future in session.pending.values():
            if not future.done():
                future.cancel()
        session.pending.clear()
        # Unblock a waiting GET listener so its stream closes.
        session.queue.put_nowait(SESSION_CLOSE)
        return True


class StatelessSessions:
    """Multi-process-safe variant: no persisted sessions, no GET channel.

    ``create`` returns a session with an empty id so the transport issues no
    ``Mcp-Session-Id`` header and treats every request as self-contained.
    """

    def create(self) -> Session:
        return Session(id="")

    def get(self, session_id: str | None) -> Session | None:
        return None

    def terminate(self, session_id: str) -> bool:
        return False
