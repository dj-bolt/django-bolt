"""
The credential sources the WebSocket documentation promises.

The handshake authenticates from the request headers. Two sources reach it:
the ``Authorization`` header, and a cookie. The cookie matters because the
browser ``WebSocket`` API cannot set headers, so a cookie is the only
credential a browser can present.

The documentation told browsers to use ``?token=<jwt>`` (issue #279). The
handshake never read the query string, so that form authenticated nothing.
These tests pin the two forms that do work, so the documentation stays true.
"""

from __future__ import annotations

import time

import jwt
import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.testing import WebSocketTestClient

SECRET = "websocket-auth-source-test-secret-hs256"


def make_token() -> str:
    return jwt.encode(
        {"sub": "1", "user_id": "1", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def api():
    api = BoltAPI()

    @api.websocket(
        "/ws/header",
        auth=[JWTAuthentication(secret=SECRET)],
        guards=[IsAuthenticated()],
    )
    async def header_ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("ok")

    @api.websocket(
        "/ws/cookie",
        auth=[JWTAuthentication(secret=SECRET, cookie="access_token")],
        guards=[IsAuthenticated()],
    )
    async def cookie_ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("ok")

    return api


@pytest.mark.asyncio
async def test_authorization_header_authenticates(api):
    """The documented form for clients that can set headers."""
    headers = {"Authorization": f"Bearer {make_token()}"}
    async with WebSocketTestClient(api, "/ws/header", headers=headers) as ws:
        assert await ws.receive_text() == "ok"


@pytest.mark.asyncio
async def test_cookie_authenticates(api):
    """The documented form for browsers, which cannot set headers."""
    headers = {"Cookie": f"access_token={make_token()}"}
    async with WebSocketTestClient(api, "/ws/cookie", headers=headers) as ws:
        assert await ws.receive_text() == "ok"


@pytest.mark.asyncio
async def test_missing_credential_is_denied(api):
    """A handshake with no credential must not reach the handler."""
    with pytest.raises(PermissionError):
        async with WebSocketTestClient(api, "/ws/cookie"):
            pass
