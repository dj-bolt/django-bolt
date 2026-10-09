"""App under test for how a rate limit picks its bucket key."""

from __future__ import annotations

from django_bolt import BoltAPI, Request
from django_bolt.middleware import rate_limit

api = BoltAPI()

# Small enough to exhaust in a few requests, slow enough to refill that the
# refill cannot hide a missing limit between two calls.
BURST = 4


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/limited")
@rate_limit(rps=1, burst=BURST, key="ip")
async def limited():
    return {"ok": True}


@api.get("/limited-by-header")
@rate_limit(rps=1, burst=BURST, key="X-Tenant")
async def limited_by_header():
    return {"ok": True}


@api.get("/remote-addr")
async def remote_addr(request: Request):
    return {"remote_addr": request.META["REMOTE_ADDR"]}


async def _client_echo(scope, receive, send):
    """A mounted ASGI app that reports the client address Bolt gave it."""
    await receive()
    body = scope["client"][0].encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": body, "more_body": False})


api.mount_asgi("/mounted", _client_echo)
