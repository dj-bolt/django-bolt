"""Bench-only app: a genuinely-awaiting async handler (async dispatch path)."""

from __future__ import annotations

import asyncio

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/probe")
async def probe():
    await asyncio.sleep(0)
    return {"ok": True}
