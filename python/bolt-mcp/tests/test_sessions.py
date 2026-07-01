"""Unit tests for SessionManager (the GET SSE listen channel's backing store).

These drive the async queue directly (no HTTP layer) — the live end-to-end SSE
stream is covered by the subprocess integration tests.
"""

from __future__ import annotations

import asyncio

from bolt_mcp.sessions import SESSION_CLOSE, SessionManager, StatelessSessions


def test_create_registers_unique_session():
    mgr = SessionManager()
    s1 = mgr.create()
    s2 = mgr.create()
    assert s1.id and s2.id and s1.id != s2.id
    assert mgr.get(s1.id) is s1
    assert mgr.get("unknown") is None


def test_terminate_removes_session_and_closes_stream():
    async def scenario():
        mgr = SessionManager()
        s = mgr.create()
        assert mgr.terminate(s.id) is True
        assert mgr.get(s.id) is None
        # A waiting GET listener is unblocked with the close sentinel.
        sentinel = await asyncio.wait_for(s.queue.get(), timeout=1.0)
        assert sentinel is SESSION_CLOSE

    asyncio.run(scenario())


def test_stateless_sessions_never_persist():
    mgr = StatelessSessions()
    assert mgr.get("anything") is None
    assert mgr.terminate("anything") is False
