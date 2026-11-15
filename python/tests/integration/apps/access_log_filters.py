"""Access-log filter app: an API with its own ``LoggingConfig`` and an API with the default config.

``/sampled`` routes belong to an API with ``sample_rate=0.0``. The other routes
take ``DJANGO_BOLT_LOG_SAMPLE`` and ``DJANGO_BOLT_LOG_SLOW_MS`` from the settings.
"""

from __future__ import annotations

import asyncio

from django_bolt import BoltAPI
from django_bolt.exceptions import HTTPException
from django_bolt.logging import LoggingConfig

api = BoltAPI()
sampled = BoltAPI(logging_config=LoggingConfig(sample_rate=0.0))


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/fast")
async def fast():
    return {"ok": True}


@api.get("/slow")
async def slow():
    await asyncio.sleep(0.3)
    return {"ok": True}


@api.get("/gone")
async def gone():
    raise HTTPException(404, detail="gone")


@sampled.get("/ok")
async def sampled_ok():
    return {"ok": True}


@sampled.get("/gone")
async def sampled_gone():
    raise HTTPException(404, detail="gone")


@sampled.get("/down")
async def sampled_down():
    raise HTTPException(503, detail="down")


api.mount("/sampled", sampled)
