"""Reload fixture (version 2).

Swapped in over ``reload_v1`` at runtime via ``ServerProject.install_api`` to
exercise ``--dev`` auto-reload.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/version")
async def version():
    return {"version": "v2"}
