"""Reload fixture (version 1).

Installed first; the reload tests then swap in ``reload_v2`` at runtime and
assert the running ``--dev`` server picks up the change.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/version")
async def version():
    return {"version": "v1"}
