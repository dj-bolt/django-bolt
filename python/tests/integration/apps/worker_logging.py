"""App under test: handlers that write log records, for the multi-process logging test.

``/pid`` gives the PID of the worker that answers. ``/warn`` writes a WARNING record. ``/boom`` raises, so Bolt logs the traceback.
When the worker stops, the lifespan writes ``SHUTDOWN_RECORDS`` WARNING records.
The worker exits immediately after them, so a record that is still in the queue is lost.
The project has no ``LOGGING`` setting, so Bolt sets up its queue-based handlers.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from django_bolt import BoltAPI

logger = logging.getLogger("worker_logging")
SHUTDOWN_RECORDS = 2000


@asynccontextmanager
async def lifespan(app: BoltAPI):
    yield
    for index in range(SHUTDOWN_RECORDS):
        logger.warning("MARKER-AT-SHUTDOWN-%d", index)


api = BoltAPI(lifespan=lifespan)


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/pid")
async def pid():
    return {"pid": os.getpid()}


@api.get("/warn")
async def warn():
    logger.warning("MARKER-WARNING-FROM-HANDLER")
    return {"ok": True}


@api.get("/boom")
async def boom():
    raise RuntimeError("MARKER-BOOM")
