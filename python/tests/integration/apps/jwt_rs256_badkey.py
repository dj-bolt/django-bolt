"""App under test proving invalid asymmetric key material aborts startup.

The ``public_key`` here is not a valid PEM, so building the Rust decoding key
at registration must fail and the ``runbolt`` server must refuse to start.
This is a copy-to-disk (``api_source``) fixture because the test asserts on
the subprocess failing to boot.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get(
    "/whoami",
    auth=[JWTAuthentication(public_key="not-a-valid-pem", algorithms=["RS256"])],
    guards=[IsAuthenticated()],
)
async def whoami(request):
    return {"user_id": request["context"]["user_id"]}
