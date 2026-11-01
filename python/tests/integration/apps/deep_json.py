"""Routes that decode deeply nested JSON on the threads that Rust starts.

msgspec uses C stack for each level of nesting. A thread with a small stack
overflows on deep data, and the process stops with SIGSEGV. ``api`` decodes on
an Actix worker thread. ``lane_api`` decodes on a request lane.
"""

from __future__ import annotations

from typing import Any

import msgspec
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Request
from django_bolt.concurrency import in_lane_mode
from django_bolt.middleware import DjangoMiddlewareStack


class Payload(msgspec.Struct):
    value: Any


api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/body")
def body(data: Payload):
    """Bolt decodes the body before the handler runs."""
    return {"ok": True}


@api.post("/decode")
def decode(request: Request):
    """The handler decodes the body itself."""
    msgspec.json.decode(request.body)
    return {"ok": True}


class _PassMiddleware(MiddlewareMixin):
    pass


# A sync route behind Django middleware runs complete on a request lane.
lane_api = BoltAPI(prefix="/lane", middleware=[DjangoMiddlewareStack([_PassMiddleware])])


@lane_api.get("/health")
async def lane_health():
    return {"status": "ok"}


@lane_api.post("/body")
def lane_body(data: Payload):
    return {"on_lane": in_lane_mode()}
