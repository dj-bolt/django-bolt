"""App under test for TestClient and runbolt reading the same settings.

``tests.integration.parity_cases`` sends the same requests to this app in
process and over a real server, and expects the same answers.
"""

from __future__ import annotations

from django_bolt import BoltAPI, Request
from django_bolt.responses import JSON

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/echo-len")
async def echo_len(request: Request):
    return {"len": len(request.body)}


@api.get("/headers")
async def headers(request: Request):
    return {"count": len(request.headers)}


@api.get("/plain")
async def plain():
    return {"ok": True}


@api.get("/remote")
async def remote(request: Request):
    return {"remote": request.META["REMOTE_ADDR"]}


@api.get("/utf8-header")
async def utf8_header():
    return JSON({"ok": True}, headers={"X-Name": "日本"})
