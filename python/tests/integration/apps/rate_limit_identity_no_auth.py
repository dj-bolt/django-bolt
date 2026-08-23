"""App under test for an identity-keyed rate limit with no auth backend.

The module imports. The server must refuse to start.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.middleware import rate_limit

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/limited-by-user")
@rate_limit(rps=1, burst=4, key="user")
async def limited_by_user():
    return {"ok": True}
