"""HTTPException.body app: typed error bodies through the real error wire."""

from __future__ import annotations

import msgspec

from django_bolt import BoltAPI
from django_bolt.exceptions import HTTPException, NotFound


class PlanRead(msgspec.Struct):
    id: int


class PlanError(msgspec.Struct):
    error: str
    code: int = 1


api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/plans/{pk}/start", response_model={200: PlanRead, 400: PlanError, 404: PlanError})
async def start_plan(request, pk: int) -> PlanRead:
    if pk == 0:
        raise HTTPException(400, body=PlanError(error="not healthy"), headers={"X-Reason": "health"})
    if pk == 1:
        raise NotFound(body=PlanError(error="missing", code=7))
    if pk == 2:
        raise HTTPException(400, detail="plain")
    return PlanRead(id=pk)


@api.post("/plans-sync/{pk}/start", response_model={200: PlanRead, 400: PlanError})
def start_plan_sync(request, pk: int) -> PlanRead:
    if pk == 0:
        raise HTTPException(400, body=PlanError(error="not healthy"))
    return PlanRead(id=pk)
