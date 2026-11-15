"""A parent API with no lifespan that mounts an API with a lifespan."""

from __future__ import annotations

from contextlib import asynccontextmanager

from django_bolt import BoltAPI

state = {"started": False}


@asynccontextmanager
async def lifespan(app: BoltAPI):
    state["started"] = True
    yield
    state["started"] = False


child = BoltAPI(lifespan=lifespan)


@child.get("/state")
async def read_state():
    return state


api = BoltAPI()
api.mount("/child", child)


@api.get("/health")
async def health():
    return {"status": "ok"}
