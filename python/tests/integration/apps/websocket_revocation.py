"""App under test: a WebSocket handshake that checks token revocation.

``/ws/jti`` uses a ``revoked_token_handler`` that takes the ``jti``.
``/ws/session`` uses one that takes the claims and checks the ``sid``. The
HTTP routes revoke a ``jti`` or end a session. A revoked token must fail the
handshake with 401, before the upgrade. ``/ws/two-backends`` has two JWT
backends with different secrets. Only the first one reads ``REVOKED_JTIS``.
"""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket
from django_bolt.auth import IsAuthenticated, JWTAuthentication

SECRET = "websocket-revocation-secret-longer-than-32-characters"
OTHER_SECRET = "websocket-revocation-other-secret-longer-than-32-chars"

REVOKED_JTIS: set[str] = set()
ENDED_SESSIONS: set[str] = set()


async def jti_revoked(jti: str) -> bool:
    return jti in REVOKED_JTIS


async def session_ended(jti: str, claims: dict) -> bool:
    return claims.get("sid") in ENDED_SESSIONS


async def never_revoked(jti: str) -> bool:
    return False


api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/revoke/{jti}")
async def revoke(jti: str):
    REVOKED_JTIS.add(jti)
    return {"revoked": jti}


@api.post("/end-session/{sid}")
async def end_session(sid: str):
    ENDED_SESSIONS.add(sid)
    return {"ended": sid}


@api.websocket(
    "/ws/jti", auth=[JWTAuthentication(secret=SECRET, revoked_token_handler=jti_revoked)], guards=[IsAuthenticated()]
)
async def by_jti(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("connected")


@api.websocket(
    "/ws/session",
    auth=[JWTAuthentication(secret=SECRET, revoked_token_handler=session_ended)],
    guards=[IsAuthenticated()],
)
async def by_session(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("connected")


@api.websocket(
    "/ws/two-backends",
    auth=[
        JWTAuthentication(secret=SECRET, revoked_token_handler=jti_revoked),
        JWTAuthentication(secret=OTHER_SECRET, revoked_token_handler=never_revoked),
    ],
    guards=[IsAuthenticated()],
)
async def two_backends(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("connected")
