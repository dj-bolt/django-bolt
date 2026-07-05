"""App under test for multi-process startup/shutdown.

Returns the worker PID so the test can confirm a ``--processes 2`` server
answers requests and shuts down cleanly.
"""

from __future__ import annotations

import os

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/pid")
async def pid():
    return {"pid": os.getpid()}
