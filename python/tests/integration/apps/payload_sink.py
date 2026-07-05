"""App under test for the streaming payload-size overflow branch.

``/sink`` takes a JSON body and ``/upload`` a single multipart file. Both exist
so a test can drive a chunked request (no Content-Length) past
``BOLT_MAX_UPLOAD_SIZE`` and assert the streaming/aggregate 413 fires — the
per-file ``File(max_size=...)`` here is deliberately far above the test payloads
so only the aggregate bound can reject them.
"""

from __future__ import annotations

import msgspec

from django_bolt import BoltAPI, UploadFile
from django_bolt.params import File

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


class Blob(msgspec.Struct):
    data: str


@api.post("/sink")
async def sink(blob: Blob) -> dict:
    return {"len": len(blob.data)}


@api.post("/upload")
async def upload(avatar: UploadFile = File(max_size=100_000)):
    return {"size": avatar.size}
