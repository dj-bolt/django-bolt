"""App under test for WebSocket↔HTTP event-loop identity.

The WebSocket handler parks on an ``asyncio.Future`` that an ordinary HTTP
POST later resolves. This is only delivered if both handlers run on the same
event loop: ``Future.set_result`` wakes its waiters with a plain same-loop
``call_soon``, which never rouses a foreign loop's selector. A framework
regression that drives WebSocket handlers on a different loop than HTTP
dispatch surfaces here as the future timing out instead of resolving.
"""

from __future__ import annotations

import asyncio

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()

# The parked future, registered by the WebSocket handler. One connection per
# server instance in the test, so a single slot is enough.
pending: dict[str, asyncio.Future] = {}


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/resolve")
async def resolve():
    future = pending.get("waiter")
    if future is None or future.done():
        return {"resolved": False}
    future.set_result("from-http")
    return {"resolved": True}


@api.websocket("/ws/wait")
async def wait_for_http(websocket: WebSocket):
    await websocket.accept()
    future = asyncio.get_running_loop().create_future()
    pending["waiter"] = future
    # Tell the test the future is registered before it fires the POST.
    await websocket.send_text("ready")
    try:
        value = await asyncio.wait_for(future, timeout=8.0)
    except TimeoutError:
        value = "timeout"
    finally:
        pending.pop("waiter", None)
    await websocket.send_text(value)
    await websocket.close()
