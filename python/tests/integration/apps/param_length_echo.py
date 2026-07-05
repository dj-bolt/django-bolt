"""App under test for max-parameter-length enforcement over a real server.

Exposes every parameter surface the limit is enforced on — HTTP path, HTTP
query, urlencoded form and multipart form — each echoing the received length so
a test can tell an accepted request (200 + length) from a rejected one (422).
"""

from __future__ import annotations

from typing import Annotated

from django_bolt import BoltAPI
from django_bolt.param_functions import Form, Query

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/echo/{value}")
async def echo_path(value: str):
    return {"length": len(value)}


@api.get("/echo-query")
async def echo_query(value: str = Query()):
    return {"length": len(value)}


@api.post("/echo-form")
async def echo_form(value: Annotated[str, Form()]):
    return {"length": len(value)}
