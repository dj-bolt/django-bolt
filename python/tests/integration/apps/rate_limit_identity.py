"""App under test for identity-keyed rate limits on a real server."""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.auth import APIKeyAuthentication
from django_bolt.middleware import rate_limit

api = BoltAPI()

BURST = 4
API_KEYS = {"key-a", "key-b"}


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/limited-by-api-key", auth=[APIKeyAuthentication(api_keys=API_KEYS, header="x-api-key")])
@rate_limit(rps=1, burst=BURST, key="api_key")
async def limited_by_api_key():
    return {"ok": True}
