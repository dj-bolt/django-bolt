"""App under test for a send after the close of the app, over real TCP.

The mounted ASGI app accepts, closes with 4000, and then sends a message.
``GET /result`` gives the error of that send.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()

results: dict[str, str] = {}


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/result")
async def result():
    return results


async def send_after_close_app(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.close", "code": 4000})
    try:
        await send({"type": "websocket.send", "text": "too late"})
    except ConnectionResetError as error:
        results["send"] = f"ConnectionResetError: {error}"
    else:
        results["send"] = "no error"


api.mount_asgi("/raw", send_after_close_app)
