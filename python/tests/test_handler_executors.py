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
from django_bolt.middleware import Middleware
from django_bolt.testing import TestClient
from tests.test_models import Article


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

    @api.get("/awaits/object-list/{item_id}")
    async def awaits_object_list(item_id: int, *, q: str) -> list[Item]:
        await asyncio.sleep(0)
        return [SimpleNamespace(id=item_id, q=q, in_task=in_task())]

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

    @api.get("/sync/untyped-list/{item_id}")
    def sync_untyped_list(item_id: int, *, q: str):
        return [item_dict(item_id, q)]

    @api.get("/sync/untyped-struct/{item_id}")
    def sync_untyped_struct(item_id: int, *, q: str):
        # Not a dict or a list, so the general serializer encodes it.
        return Item(id=item_id, q=q, in_task=in_task())

    @api.get("/sync/object-list/{item_id}")
    def sync_object_list(item_id: int, *, q: str) -> list[Item]:
        return [SimpleNamespace(id=item_id, q=q, in_task=in_task())]


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
        ("/awaits/object-list/7", [TASK]),
        ("/awaits/int-list/7", [7, 4]),
        ("/sync/untyped/7", NO_TASK),
        ("/sync/dict/7", NO_TASK),
        ("/sync/struct/7", NO_TASK),
        ("/sync/struct-list/7", [NO_TASK]),
        ("/sync/untyped-list/7", [NO_TASK]),
        ("/sync/untyped-struct/7", NO_TASK),
        ("/sync/object-list/7", [NO_TASK]),
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

    @api.get("/awaits/object-list")
    async def awaits_object_list() -> list[Item]:
        await asyncio.sleep(0)
        return [SimpleNamespace(id=1, q="none", in_task=in_task())]

    @api.get("/awaits/untyped-list")
    async def awaits_untyped_list():
        await asyncio.sleep(0)
        return [item_dict(1, "none")]

    @api.get("/sync/untyped")
    def sync_untyped():
        return item_dict(1, "none")

    @api.get("/sync/untyped-list")
    def sync_untyped_list():
        return [item_dict(1, "none")]

    @api.get("/sync/dict")
    def sync_dict() -> dict:
        return item_dict(1, "none")

    @api.get("/sync/struct")
    def sync_struct() -> Item:
        return Item(id=1, q="none", in_task=in_task())

    @api.get("/sync/struct-list")
    def sync_struct_list() -> list[Item]:
        return [Item(id=1, q="none", in_task=in_task())]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/trivial/struct", {"id": 1, "q": "none", "in_task": False}),
        ("/awaits/struct", {"id": 1, "q": "none", "in_task": True}),
        ("/awaits/object-list", [{"id": 1, "q": "none", "in_task": True}]),
        ("/awaits/untyped-list", [{"id": 1, "q": "none", "in_task": True}]),
        ("/sync/untyped", {"id": 1, "q": "none", "in_task": False}),
        ("/sync/untyped-list", [{"id": 1, "q": "none", "in_task": False}]),
        ("/sync/dict", {"id": 1, "q": "none", "in_task": False}),
        ("/sync/struct", {"id": 1, "q": "none", "in_task": False}),
        ("/sync/struct-list", [{"id": 1, "q": "none", "in_task": False}]),
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


class PassThrough(Middleware):
    async def process_request(self, request):
        return await self.get_response(request)


def in_a_task(expected):
    """Return ``expected`` with ``in_task`` true: behind middleware, the handler runs in the task of the request."""
    if isinstance(expected, list):
        return [in_a_task(item) for item in expected]
    return {**expected, "in_task": True}


SYNC_CASES = [
    ("/sync/untyped/7", NO_TASK),
    ("/sync/dict/7", NO_TASK),
    ("/sync/struct/7", NO_TASK),
    ("/sync/struct-list/7", [NO_TASK]),
    ("/sync/untyped-list/7", [NO_TASK]),
    ("/sync/untyped-struct/7", NO_TASK),
    ("/sync/object-list/7", [NO_TASK]),
    ("/sync/untyped", {"id": 1, "q": "none", "in_task": False}),
    ("/sync/untyped-list", [{"id": 1, "q": "none", "in_task": False}]),
    ("/sync/dict", {"id": 1, "q": "none", "in_task": False}),
    ("/sync/struct", {"id": 1, "q": "none", "in_task": False}),
    ("/sync/struct-list", [{"id": 1, "q": "none", "in_task": False}]),
]


@pytest.mark.parametrize(("path", "expected"), SYNC_CASES)
def test_the_async_executor_of_a_sync_handler_keeps_the_response(path, expected):
    """A route with Python middleware cannot take the sync dispatch, so it uses the async executor."""
    api = BoltAPI(middleware=[PassThrough])
    register_routes(api)
    register_no_param_routes(api)

    with TestClient(api) as client:
        response = client.get(path, params={"q": "bolt"})

    assert response.status_code == 200, response.text
    assert response.json() == in_a_task(expected)


def titles(prefix: str = ""):
    """Return a QuerySet from a helper, where the analysis of the handler cannot see the ORM."""
    return Article.objects.filter(title__startswith=prefix).values("title")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("middleware", [[], [PassThrough]], ids=["sync-dispatch", "async-executor"])
def test_a_queryset_from_a_sync_handler_loads(middleware):
    api = BoltAPI(middleware=middleware)

    @api.get("/titles")
    def all_titles():
        return titles()

    @api.get("/titles/{prefix}")
    def some_titles(prefix: str, *, q: str):
        return titles(prefix)

    Article.objects.create(title="One", content="First", author="A")
    with TestClient(api) as client:
        plain = client.get("/titles")
        filtered = client.get("/titles/O", params={"q": "bolt"})

    assert plain.json() == [{"title": "One"}], plain.text
    assert filtered.json() == [{"title": "One"}], filtered.text
