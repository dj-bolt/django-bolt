"""App under test for trusted-proxy client-IP rate limiting (issue #302).

One route with a small per-IP limit. The server-integration tests hit it over
real TCP, so the peer address is the loopback IP, and prove that
``X-Forwarded-For`` is only honored when ``BOLT_TRUSTED_PROXIES`` trusts the
peer.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.middleware import rate_limit

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/limited")
@rate_limit(rps=1, burst=3, key="ip")
async def limited():
    return {"ok": True}
