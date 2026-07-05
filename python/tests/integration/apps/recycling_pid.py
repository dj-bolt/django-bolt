"""App under test for worker recycling (lifetime/max-rss/respawn).

Exposes the worker PID so a test can confirm a recycle happened (the PID
changes) and a ``/die`` route that crashes the worker mid-request so the
respawn path can be exercised.
"""

from __future__ import annotations

import os

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/pid")
async def pid():
    return {"pid": os.getpid()}


@api.get("/die")
async def die():
    os._exit(1)
