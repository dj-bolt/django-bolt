"""App under test for per-route CORS on streaming (SSE) responses.

Covers async and sync generators, credentialed and wildcard origins, a route
with no ``@cors`` decorator, and a slow generator for real-time streaming.
"""

from __future__ import annotations

import asyncio
import time

from django_bolt import BoltAPI, StreamingResponse
from django_bolt.middleware import cors

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/sse-cors-async")
@cors(origins=["https://example.com", "https://trusted.com"])
async def sse_cors_async():
    async def gen():
        for i in range(3):
            yield f"data: message-{i}\n\n"
            await asyncio.sleep(0.01)

    return StreamingResponse(gen(), media_type="text/event-stream")


@api.get("/sse-cors-sync")
@cors(origins=["https://sync-app.com"])
async def sse_cors_sync():
    def gen():
        for i in range(3):
            yield f"data: sync-{i}\n\n"
            time.sleep(0.01)

    return StreamingResponse(gen(), media_type="text/event-stream")


@api.get("/sse-cors-credentials")
@cors(origins=["https://secure.com"], credentials=True)
async def sse_cors_credentials():
    async def gen():
        yield "data: secure\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@api.get("/sse-cors-wildcard")
@cors(origins=["*"])
async def sse_cors_wildcard():
    async def gen():
        yield "data: public\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@api.get("/sse-no-cors")
async def sse_no_cors():
    async def gen():
        yield "data: no-cors\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@api.get("/sse-timing")
async def sse_timing():
    async def gen():
        for i in range(3):
            yield f"data: timing-{i}\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream")
