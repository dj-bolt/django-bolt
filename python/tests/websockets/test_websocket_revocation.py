"""A revoked token fails the WebSocket handshake of ``WebSocketTestClient``.

The real-server counterpart is ``integration/test_websocket_revocation_server_integration.py``.
"""

from __future__ import annotations

import time

import jwt
import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.testing import WebSocketTestClient

SECRET = "websocket-revocation-secret-longer-than-32-characters"


def _bearer(jti: str, sid: str) -> dict[str, str]:
    token = jwt.encode({"sub": "1", "jti": jti, "sid": sid, "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("takes_claims", [False, True], ids=["jti", "claims"])
async def test_a_revoked_token_fails_the_websocket_handshake(takes_claims):
    revoked: set[str] = set()
    seen: list[dict] = []

    if takes_claims:

        async def handler(jti: str, claims: dict) -> bool:
            seen.append(claims)
            return claims["sid"] in revoked
    else:

        async def handler(jti: str) -> bool:
            return jti in revoked

    api = BoltAPI()

    @api.websocket(
        "/ws", auth=[JWTAuthentication(secret=SECRET, revoked_token_handler=handler)], guards=[IsAuthenticated()]
    )
    async def endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    async def connect(headers):
        async with WebSocketTestClient(
            api, "/ws", headers=headers, cors_allowed_origins=["*"], read_django_settings=False
        ) as websocket:
            return await websocket.receive_text()

    headers = _bearer("jti-1", "sid-1")
    assert await connect(headers) == "connected"
    if takes_claims:
        assert seen[0]["sid"] == "sid-1"

    revoked.add("sid-1" if takes_claims else "jti-1")
    with pytest.raises(PermissionError, match="revoked"):
        await connect(headers)
    assert await connect(_bearer("jti-2", "sid-2")) == "connected"
