"""App under test for identity-keyed rate limits on a real server."""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication
from django_bolt.middleware import rate_limit

api = BoltAPI()

BURST = 4
SECRET = "rate-limit-identity-server-secret-32b"
API_KEYS = {"key-a", "key-b"}
JWT = [JWTAuthentication(secret=SECRET)]
KEY = [APIKeyAuthentication(api_keys=API_KEYS, header="x-api-key")]


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/limited-by-user", auth=JWT)
@rate_limit(rps=1, burst=BURST, key="user")
async def limited_by_user():
    return {"ok": True}


@api.get("/limited-by-user-guarded", auth=JWT, guards=[IsAuthenticated()])
@rate_limit(rps=1, burst=BURST, key="user")
async def limited_by_user_guarded():
    return {"ok": True}


@api.get("/limited-by-api-key", auth=KEY)
@rate_limit(rps=1, burst=BURST, key="api_key")
async def limited_by_api_key():
    return {"ok": True}


@api.websocket("/ws/limited-by-api-key", auth=KEY)
@rate_limit(rps=1, burst=BURST, key="api_key")
async def ws_limited_by_api_key(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("connected")
    # The client closes. A server-side close right after send_text can
    # overtake the text frame on the wire.
    async for _ in websocket.iter_text():
        pass
