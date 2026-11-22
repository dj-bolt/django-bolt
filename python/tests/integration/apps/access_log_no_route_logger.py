"""Access-log app whose first API has no route of its own and mounts an API with ``logger_name``.

The first API has no docs routes either. It gives only the 404 of a path with no route,
so the health path is ``/named/health``.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig
from django_bolt.openapi import OpenAPIConfig

api = BoltAPI(openapi_config=OpenAPIConfig(title="API", version="1.0.0", enabled=False))
named = BoltAPI(logging_config=LoggingConfig(logger_name="myapp.api"))


@named.get("/health")
async def health():
    return {"status": "ok"}


@named.get("/ping")
async def ping():
    return {"ok": True}


api.mount("/named", named)
