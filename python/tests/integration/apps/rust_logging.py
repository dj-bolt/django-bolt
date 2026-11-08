"""App under test: a mounted ASGI app that raises, so the Rust core logs an ERROR record."""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


async def failing_app(scope, receive, send):
    raise RuntimeError("boom from the mounted app")


api.mount_asgi("/mounted", failing_app)
