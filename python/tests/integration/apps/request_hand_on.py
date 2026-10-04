"""App under test for a request that the handler hands on.

Each handler has a request parameter and one more parameter, so Bolt reads
its source to find the request parts it uses. Each handler hands the request
on: to a helper, to ``super().create(request)``, to another name, to a dict or
to an attribute. The helper reads the body, so each route must get it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated

from django_bolt import BoltAPI, Depends, ViewSet

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


def _current_user() -> dict:
    return {"id": 1}


def _read_body(request) -> bytes:
    return request.body


def _read_context_body(context: dict) -> bytes:
    return context["request"].body


@api.post("/positional")
async def positional(request, user: Annotated[dict, Depends(_current_user)]):
    return {"body": _read_body(request).decode(), "user": user["id"]}


@api.post("/keyword")
async def keyword(request, user: Annotated[dict, Depends(_current_user)]):
    return {"body": _read_body(request=request).decode(), "user": user["id"]}


@api.post("/sync")
def sync(request, user: Annotated[dict, Depends(_current_user)]):
    return {"body": _read_body(request).decode(), "user": user["id"]}


@api.post("/alias")
async def alias(request, user: Annotated[dict, Depends(_current_user)]):
    req = request
    return {"body": _read_body(req).decode(), "user": user["id"]}


@api.post("/container")
async def container(request, user: Annotated[dict, Depends(_current_user)]):
    return {"body": _read_context_body({"request": request}).decode(), "user": user["id"]}


@api.post("/attribute")
async def attribute(request, user: Annotated[dict, Depends(_current_user)]):
    holder = SimpleNamespace()
    holder.request = request
    return {"body": _read_body(holder.request).decode(), "user": user["id"]}


@api.post("/path/{user_id}")
async def path_param(request, user_id: int):
    req = request
    return {"body": _read_body(req).decode(), "user": user_id}


class Base(ViewSet):
    async def create(self, request):
        return {"body": request.body.decode()}


@api.viewset("/items")
class Items(Base):
    async def create(self, request, user: Annotated[dict, Depends(_current_user)]):
        return await super().create(request)
