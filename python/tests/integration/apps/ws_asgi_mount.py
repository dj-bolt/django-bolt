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

    # Channels always sends the subprotocol key, often as None.
    await send({"type": "websocket.accept", "subprotocol": None})

    headers = {name.decode(): value.decode() for name, value in scope["headers"]}
    await send(
        {
            "type": "websocket.send",
            "text": json.dumps(
                {
                    "scheme": scope["scheme"],
                    "path": scope["path"],
                    "raw_path": scope["raw_path"].decode(),
                    "root_path": scope["root_path"],
                    "query_string": scope["query_string"].decode(),
                    "authorization": headers.get("authorization"),
                    "x_name": headers.get("x-name"),
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


async def origin_guarded_app(scope, receive, send):
    """A delegated application that deliberately validates its own Origin."""
    origins = [value for name, value in scope["headers"] if name == b"origin"]
    if not origins or any(value != b"https://delegated.example" for value in origins):
        await receive()
        await send({"type": "websocket.close"})
        return
    if scope["query_string"] == b"report_origins":
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": json.dumps([v.decode() for v in origins])})
        return
    await echo_app(scope, receive, send)


api.mount_asgi("/mounted", echo_app)
api.mount_asgi(
    "/restricted",
    echo_app,
    websocket_origin_policy="allowlist",
    websocket_allowed_origins=["https://restricted.example"],
)
api.mount_asgi("/delegated", origin_guarded_app, websocket_origin_policy="delegate")
api.mount_django("/django")
