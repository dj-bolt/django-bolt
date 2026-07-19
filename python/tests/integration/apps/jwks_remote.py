"""App configured with an HTTPS-hosted JWKS fetched at server startup."""

from __future__ import annotations

import os

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication

ISSUER = "https://issuer.example.test/"
AUDIENCE = "django-bolt-integration"
JWKS_URL = os.environ.get(
    "DJANGO_BOLT_TEST_JWKS_URL",
)

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/configuration")
async def configuration():
    return {"jwks_url_configured": JWKS_URL is not None}


if JWKS_URL is not None:

    @api.get(
        "/whoami",
        auth=[
            JWTAuthentication(
                jwks_url=JWKS_URL,
                algorithms=["RS256"],
                issuer=ISSUER,
                audience=AUDIENCE,
            )
        ],
        guards=[IsAuthenticated()],
    )
    async def whoami(request):
        return {"user_id": request["context"]["user_id"]}
