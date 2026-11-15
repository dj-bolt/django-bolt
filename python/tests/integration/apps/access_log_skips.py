"""Access-log skip app: an API with ``skip_paths`` and ``skip_status_codes``, and an API with the default config.

The default config skips ``/health``, ``/ready`` and ``/metrics``.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.exceptions import HTTPException
from django_bolt.logging import LoggingConfig

api = BoltAPI()
quiet = BoltAPI(
    logging_config=LoggingConfig(skip_paths={"/quiet/ping", "/quiet/items/7"}, skip_status_codes={204, 404})
)


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/ready")
async def ready():
    raise HTTPException(503, detail="not ready")


@api.get("/open")
async def open_route():
    return {"ok": True}


@quiet.get("/ping")
async def ping():
    return {"ok": True}


@quiet.get("/health")
async def quiet_health():
    return {"ok": True}


@quiet.get("/items/{item_id}")
async def item(item_id: int):
    return {"item_id": item_id}


@quiet.delete("/items/{item_id}", status_code=204)
async def delete_item(item_id: int):
    return None


@quiet.get("/gone")
async def gone():
    raise HTTPException(404, detail="gone")


@quiet.get("/down")
async def down():
    raise HTTPException(503, detail="down")


api.mount("/quiet", quiet)
