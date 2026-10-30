"""Native-route app: one route served by a Rust handler from a capsule."""

from __future__ import annotations

from django_bolt import BoltAPI, _core

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


api.native_route("GET", "/native/{id}", _core.native_test_capsule())
