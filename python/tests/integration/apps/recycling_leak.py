"""A deliberately leaky app under test for ``--max-rss`` recycling.

Each ``/leak`` request retains memory forever, so a worker's RSS climbs without
bound until ``--max-rss`` recycles it. 16 KiB/request makes RSS climb fast
enough that, without recycling, workers would blow far past the limit within the
load window — so a worker seen back near baseline is unambiguous proof that
recycling reset it.
"""

from __future__ import annotations

import os

from django_bolt import BoltAPI

api = BoltAPI()

_LEAK: list[bytearray] = []


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/leak")
async def leak():
    _LEAK.append(bytearray(16384))  # retain 16 KiB per request == a memory leak
    return {"pid": os.getpid(), "chunks": len(_LEAK)}
