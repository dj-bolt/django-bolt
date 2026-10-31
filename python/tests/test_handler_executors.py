"""Bolt compiles one executor for each route. Each executor must pass every argument and keep the response.

The executor depends on the handler: sync or async, with or without an await,
with or without parameters, and on the response type. Each test registers its
own routes, because Bolt compiles the executor when a route registers. A
handler reads ``asyncio.current_task()`` to show which dispatch ran: a sync
handler, and an async handler with no await, run with no asyncio task.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


def in_task() -> bool:
    try:
        return asyncio.current_task() is not None
    except RuntimeError:
        return False


class Item(msgspec.Struct):
    id: int
    q: str
    in_task: bool


def item_dict(item_id: int, q: str) -> dict:
    return {"id": item_id, "q": q, "in_task": in_task()}


def register_routes(api: BoltAPI) -> None:
    """Register one route for each executor. ``item_id`` is positional, ``q`` is keyword-only."""

    @api.get("/trivial/untyped/{item_id}")
    async def trivial_untyped(item_id: int, *, q: str):
        return item_dict(item_id, q)

    @api.get("/trivial/untyped-list/{item_id}")
    async def trivial_untyped_list(item_id: int, *, q: str):
        return [item_dict(item_id, q)]

    @api.get("/trivial/dict/{item_id}")
    async def trivial_dict(item_id: int, *, q: str) -> dict:
        return item_dict(item_id, q)

    @api.get("/trivial/struct/{item_id}")
    async def trivial_struct(item_id: int, *, q: str) -> Item:
        return Item(id=item_id, q=q, in_task=in_task())

    @api.get("/trivial/struct-list/{item_id}")
    async def trivial_struct_list(item_id: int, *, q: str) -> list[Item]:
        return [Item(id=item_id, q=q, in_task=in_task())]

    @api.get("/trivial/object-list/{item_id}")
    async def trivial_object_list(item_id: int, *, q: str) -> list[Item]:
        # An object converts by attribute, on the slow path.
        return [SimpleNamespace(id=item_id, q=q, in_task=in_task())]

    @api.get("/awaits/untyped/{item_id}")
    async def awaits_untyped(item_id: int, *, q: str):
        await asyncio.sleep(0)
        return item_dict(item_id, q)

    @api.get("/awaits/dict/{item_id}")
    async def awaits_dict(item_id: int, *, q: str) -> dict:
        await asyncio.sleep(0)
        return item_dict(item_id, q)

    @api.get("/awaits/struct/{item_id}")
    async def awaits_struct(item_id: int, *, q: str) -> Item:
        await asyncio.sleep(0)
        return Item(id=item_id, q=q, in_task=in_task())

    @api.get("/awaits/struct-from-dict/{item_id}")
    async def awaits_struct_from_dict(item_id: int, *, q: str) -> Item:
        # A dict is not an Item, so the slow path validates it.
        await asyncio.sleep(0)
        return item_dict(item_id, q)

    @api.get("/awaits/struct-list/{item_id}")
    async def awaits_struct_list(item_id: int, *, q: str) -> list[Item]:
        await asyncio.sleep(0)
        return [Item(id=item_id, q=q, in_task=in_task())]

    @api.get("/awaits/int-list/{item_id}")
    async def awaits_int_list(item_id: int, *, q: str) -> list[int]:
        # A validated type that the inline fast path does not take.
        await asyncio.sleep(0)
        return [item_id, len(q)]

    @api.get("/sync/untyped/{item_id}")
    def sync_untyped(item_id: int, *, q: str):
        return item_dict(item_id, q)

    @api.get("/sync/dict/{item_id}")
    def sync_dict(item_id: int, *, q: str) -> dict:
        return item_dict(item_id, q)

    @api.get("/sync/struct/{item_id}")
    def sync_struct(item_id: int, *, q: str) -> Item:
        return Item(id=item_id, q=q, in_task=in_task())

    @api.get("/sync/struct-list/{item_id}")
    def sync_struct_list(item_id: int, *, q: str) -> list[Item]:
        return [Item(id=item_id, q=q, in_task=in_task())]


ITEM = {"id": 7, "q": "bolt"}
NO_TASK = {**ITEM, "in_task": False}
TASK = {**ITEM, "in_task": True}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/trivial/untyped/7", NO_TASK),
        ("/trivial/untyped-list/7", [NO_TASK]),
        ("/trivial/dict/7", NO_TASK),
        ("/trivial/struct/7", NO_TASK),
        ("/trivial/struct-list/7", [NO_TASK]),
        ("/trivial/object-list/7", [NO_TASK]),
        ("/awaits/untyped/7", TASK),
        ("/awaits/dict/7", TASK),
        ("/awaits/struct/7", TASK),
        ("/awaits/struct-from-dict/7", TASK),
        ("/awaits/struct-list/7", [TASK]),
        ("/awaits/int-list/7", [7, 4]),
        ("/sync/untyped/7", NO_TASK),
        ("/sync/dict/7", NO_TASK),
        ("/sync/struct/7", NO_TASK),
        ("/sync/struct-list/7", [NO_TASK]),
    ],
)
def test_each_executor_passes_positional_and_keyword_only_arguments(path, expected):
    api = BoltAPI()
    register_routes(api)

    with TestClient(api) as client:
        response = client.get(path, params={"q": "bolt"})

    assert response.status_code == 200, response.text
    assert response.json() == expected


def register_no_param_routes(api: BoltAPI) -> None:
    """Register one route for each executor of a handler with no parameters."""

    @api.get("/trivial/struct")
    async def trivial_struct() -> Item:
        return Item(id=1, q="none", in_task=in_task())

    @api.get("/awaits/struct")
    async def awaits_struct() -> Item:
        await asyncio.sleep(0)
        return Item(id=1, q="none", in_task=in_task())

    @api.get("/awaits/untyped-list")
    async def awaits_untyped_list():
        await asyncio.sleep(0)
        return [item_dict(1, "none")]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/trivial/struct", {"id": 1, "q": "none", "in_task": False}),
        ("/awaits/struct", {"id": 1, "q": "none", "in_task": True}),
        ("/awaits/untyped-list", [{"id": 1, "q": "none", "in_task": True}]),
    ],
)
def test_each_executor_of_a_handler_with_no_parameters_keeps_the_response(path, expected):
    api = BoltAPI()
    register_no_param_routes(api)

    with TestClient(api) as client:
        response = client.get(path)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.json() == expected
