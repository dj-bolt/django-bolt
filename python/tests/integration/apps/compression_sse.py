"""App under test for one CompressionConfig driving both buffered + SSE.

A buffered JSON route and an SSE route share the single ``BOLT_COMPRESSION``
config wired in the test's ``settings_extra``.
"""

from __future__ import annotations

import asyncio

from django_bolt import BoltAPI
from django_bolt.responses import EventSourceResponse

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/buffered")
async def buffered():
    return {"payload": "x" * 2000}


@api.get("/sse")
async def sse():
    async def gen():
        for i in range(3):
            yield {"i": i}
            await asyncio.sleep(0)

    return EventSourceResponse(gen())
