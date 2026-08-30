"""HTTPException.body: a typed error body, encoded as-is."""

from __future__ import annotations

import msgspec

from django_bolt import BoltAPI
from django_bolt.exceptions import HTTPException, NotFound
from django_bolt.testing import TestClient


class PlanRead(msgspec.Struct):
    id: int


class PlanError(msgspec.Struct):
    error: str
    code: int = 1


api = BoltAPI()


@api.post("/plans/{pk}/start", response_model={200: PlanRead, 400: PlanError})
async def start_plan(request, pk: int) -> PlanRead:
    if pk == 0:
        raise HTTPException(400, body=PlanError(error="not healthy"), headers={"X-Reason": "health"})
    if pk == 1:
        raise NotFound(body=PlanError(error="missing", code=7))
    if pk == 2:
        raise HTTPException(400, detail="plain")
    return PlanRead(id=pk)


def test_body_is_encoded_as_is():
    with TestClient(api) as client:
        r = client.post("/plans/0/start")
    assert r.status_code == 400
    assert r.json() == {"error": "not healthy", "code": 1}
    assert r.headers["x-reason"] == "health"
    assert r.headers["content-type"].startswith("application/json")


def test_subclass_carries_body():
    with TestClient(api) as client:
        r = client.post("/plans/1/start")
    assert r.status_code == 404
    assert r.json() == {"error": "missing", "code": 7}


def test_no_body_keeps_detail_envelope():
    with TestClient(api) as client:
        r = client.post("/plans/2/start")
    assert r.status_code == 400
    assert r.json() == {"detail": "plain"}


def test_body_attribute_default_is_none():
    assert HTTPException(400).body is None
