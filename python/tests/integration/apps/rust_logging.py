"""App under test: a mounted ASGI app that raises, so the Rust core logs an ERROR record.

``/ok`` is a normal route. A request to it writes no Rust record.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/ok")
async def ok():
    return {"ok": True}


async def failing_app(scope, receive, send):
    raise RuntimeError("boom from the mounted app")


api.mount_asgi("/mounted", failing_app)
