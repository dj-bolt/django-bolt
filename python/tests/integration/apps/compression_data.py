"""App under test for buffered (non-streaming) compression.

A large JSON route (above the default compression threshold) plus a tiny one
(below it). Compression itself is configured via Django settings
(``BOLT_COMPRESSION``) in the test's ``settings_extra``.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/data")
async def get_data():
    return {"payload": "x" * 2000}


@api.get("/tiny")
async def tiny():
    return {"x": "small"}
