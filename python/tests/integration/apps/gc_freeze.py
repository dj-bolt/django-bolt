"""An app that reports the state of the garbage collector of its process.

``runbolt`` takes the objects of the startup out of the garbage collector: see
``freeze_startup_objects`` in ``django_bolt._gc``.
"""

from __future__ import annotations

import gc
import os

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/collector")
async def collector():
    return {"pid": os.getpid(), "frozen": gc.get_freeze_count()}
