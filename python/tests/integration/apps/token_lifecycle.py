"""Real-server app for the access/refresh token lifecycle."""

from __future__ import annotations

from django_bolt import JSON, BoltAPI
from django_bolt.auth import (
    InMemoryRevocation,
    IsAuthenticated,
    JWTAuthentication,
    TokenRotationError,
    create_token_pair,
    rotate_refresh_token,
    set_token_cookies,
)
from django_bolt.exceptions import Unauthorized

SECRET = "token-lifecycle-server-integration-secret"
USER_ID = "lifecycle-user"

store = InMemoryRevocation()
api = BoltAPI()


def pair_payload(pair):
    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
    }


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/login")
async def login():
    return pair_payload(create_token_pair(USER_ID, secret=SECRET))


@api.post("/login-cookies")
async def login_cookies():
    pair = create_token_pair(USER_ID, secret=SECRET)
    response = JSON({"status": "ok"})
    return set_token_cookies(
        response,
        pair,
        secure=False,
        refresh_path="/refresh",
    )


@api.post(
    "/refresh",
    auth=[
        JWTAuthentication(
            secret=SECRET,
            token_type="refresh",
            require_jti=True,
        )
    ],
    guards=[IsAuthenticated()],
)
async def refresh(request):
    try:
        pair = await rotate_refresh_token(
            request["context"]["auth_claims"],
            store=store,
            secret=SECRET,
        )
    except TokenRotationError:
        raise Unauthorized(detail="Invalid refresh token") from None
    return pair_payload(pair)


@api.get(
    "/whoami",
    auth=[JWTAuthentication(secret=SECRET)],
    guards=[IsAuthenticated()],
)
async def whoami(request):
    return {"user_id": request["context"]["user_id"]}
