"""App under test for default-on streaming compression.

Each route uses a plain streaming response with no ``compress=`` kwarg, relying
on the default ``BoltAPI().compression``. ``/events/raw`` opts out via
``@no_compress``.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.middleware import no_compress
from django_bolt.responses import EventSourceResponse, StreamingResponse

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/events")
async def stream():
    async def gen():
        for i in range(5):
            yield {"i": i}

    return EventSourceResponse(gen())


@api.get("/events/raw")
@no_compress
async def raw_stream():
    async def gen():
        for i in range(3):
            yield {"i": i}

    return EventSourceResponse(gen())


@api.get("/text")
async def text_stream():
    async def gen():
        for chunk in ("alpha-", "beta-", "gamma"):
            yield chunk

    return StreamingResponse(gen(), media_type="text/plain")
