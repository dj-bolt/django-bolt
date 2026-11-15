"""App under test: a config decorator above the route decorator stops startup.

The route decorator reads the configuration of the handler, so ``@rate_limit``
above it has no effect. ``runbolt`` must refuse to start. The test reads the
output of the subprocess that fails, so it copies this file to the disk
(``api_source``).
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.middleware import rate_limit

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@rate_limit(rps=1, burst=1)
@api.get("/limited")
async def limited():
    return {"ok": True}
