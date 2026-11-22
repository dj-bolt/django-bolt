"""Access-log logger app: an API with the default logger, and a mounted API with ``logger_name``."""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.exceptions import HTTPException
from django_bolt.logging import LoggingConfig

api = BoltAPI()
named = BoltAPI(logging_config=LoggingConfig(logger_name="myapp.api"))


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/open")
async def open_route():
    return {"ok": True}


@api.get("/broken")
async def broken():
    raise HTTPException(404, detail="broken")


@named.get("/ping")
async def ping():
    return {"ok": True}


@named.get("/gone")
async def gone():
    raise HTTPException(404, detail="gone")


api.mount("/named", named)
