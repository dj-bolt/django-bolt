"""App under test for client disconnects against an ASGI mount.

``/mounted/wait`` parks in ``receive()`` and records what it gets.
``/mounted/stream`` sends headers, then streams until ``receive()`` returns.
``/events`` reports what the mounted app observed.
"""

from __future__ import annotations

import asyncio

from django_bolt import BoltAPI

api = BoltAPI()
events: list[str] = []


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/events")
async def get_events():
    return {"events": events}


async def mounted(scope, receive, send):
    await receive()  # request body
    if scope["path"] == "/wait":
        try:
            message = await asyncio.wait_for(receive(), timeout=3)
            events.append(message["type"])
        except TimeoutError:
            events.append("timeout")
        return

    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    disconnect = asyncio.ensure_future(receive())
    while not disconnect.done():
        await send({"type": "http.response.body", "body": b"tick\n", "more_body": True})
        await asyncio.sleep(0.05)
    events.append(disconnect.result()["type"])
    # The client is gone. These sends must be silent no-ops.
    await send({"type": "http.response.body", "body": b"", "more_body": False})
    events.append("late-send-ok")


api.mount_asgi("/mounted", mounted)
