"""App under test for the graceful-shutdown window.

``/slow`` waits longer than the window of the test. The test sends SIGTERM
while the request runs and measures the time until the server exits.
"""

from __future__ import annotations

import asyncio

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/slow")
async def slow(seconds: float):
    await asyncio.sleep(seconds)
    return {"slept": seconds}
