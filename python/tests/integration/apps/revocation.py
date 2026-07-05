"""App under test for JWT token revocation over a real server.

Defines a JWT-protected route plus a ``/logout`` endpoint that revokes the
token's JTI via an in-memory store. ``/login`` issues tokens with random JTIs
and exposes ``exp`` so the test client can reuse them. ``SECRET`` is exported so
tests can mint their own tokens (e.g. one without a ``jti`` claim).
"""

from __future__ import annotations

import time
import uuid

import jwt

from django_bolt import BoltAPI
from django_bolt.auth import InMemoryRevocation, IsAuthenticated, JWTAuthentication

SECRET = "revocation-server-integration-secret"

api = BoltAPI()
revocation = InMemoryRevocation()
auth = JWTAuthentication(secret=SECRET, revocation_store=revocation)


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/login")
async def login():
    jti = uuid.uuid4().hex
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "1",
            "jti": jti,
            "iat": now,
            "exp": now + 3600,
            "is_staff": False,
            "is_superuser": False,
            "username": "subject",
        },
        SECRET,
        algorithm="HS256",
    )
    return {"token": token, "jti": jti}


@api.get("/protected", auth=[auth], guards=[IsAuthenticated()])
async def protected(request):
    return {"ok": True, "user_id": request["context"].get("user_id")}


@api.post("/logout", auth=[auth], guards=[IsAuthenticated()])
async def logout(request):
    claims = request["context"]["auth_claims"]
    await revocation.revoke(claims["jti"], exp=claims.get("exp"))
    return {"status": "logged_out"}
