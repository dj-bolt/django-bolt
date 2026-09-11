"""App under test for WebSocket dispatch to a mounted ASGI application.

Mounts a raw ASGI app that echoes text frames. The app reports the scope keys
it received, so the test can check the scope follows the ASGI specification.
"""

from __future__ import annotations

import json

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.websocket("/ws/direct")
async def direct(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("route")


async def echo_app(scope, receive, send):
    if scope["type"] != "websocket":
        raise AssertionError(f"expected a websocket scope, got {scope['type']}")

    connect = await receive()
    if connect["type"] != "websocket.connect":
        raise AssertionError(f"expected websocket.connect, got {connect['type']}")

    await send({"type": "websocket.accept"})

    headers = {name.decode(): value.decode() for name, value in scope["headers"]}
    await send(
        {
            "type": "websocket.send",
            "text": json.dumps(
                {
                    "path": scope["path"],
                    "root_path": scope["root_path"],
                    "query_string": scope["query_string"].decode(),
                    "authorization": headers.get("authorization"),
                }
            ),
        }
    )

    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return
        text = message.get("text")
        if text == "bye":
            await send({"type": "websocket.close", "code": 1000, "reason": "done"})
            return
        await send({"type": "websocket.send", "text": f"echo:{text}"})


api.mount_asgi("/mounted", echo_app)
