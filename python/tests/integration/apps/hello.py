"""Minimal app under test: one route that round-trips a JSON body.

Used by the runbolt smoke tests to prove the server (and ``--dev`` mode) serves
an HTTP request end-to-end.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/hello")
async def hello():
    return {"message": "hello"}
