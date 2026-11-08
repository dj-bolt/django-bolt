"""App under test: streaming bodies that raise after the first chunk."""

from __future__ import annotations

from django_bolt import BoltAPI, StreamingResponse

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


def sync_body():
    yield "first,"
    raise ValueError("boom in the sync body")


async def async_body():
    yield "first,"
    raise ValueError("boom in the async body")


@api.get("/sync")
async def sync_stream():
    return StreamingResponse(sync_body(), media_type="text/plain")


@api.get("/async")
async def async_stream():
    return StreamingResponse(async_body(), media_type="text/plain")
