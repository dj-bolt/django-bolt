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

from django_bolt import BoltAPI, Depends
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

    @api.get("/sync/struct-from-dict/{item_id}")
    def sync_struct_from_dict(item_id: int, *, q: str) -> Item:
        return item_dict(item_id, q)

    @api.get("/sync/struct-list-from-tuple/{item_id}")
    def sync_struct_list_from_tuple(item_id: int, *, q: str) -> list[Item]:
        # A tuple is not a list, so the slow path validates it.
        return (item_dict(item_id, q),)


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
        ("/sync/struct-from-dict/7", NO_TASK),
        ("/sync/struct-list-from-tuple/7", [NO_TASK]),
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

    @api.get("/sync/object-list")
    def sync_object_list() -> list[Item]:
        return [SimpleNamespace(id=1, q="none", in_task=in_task())]

    @api.get("/sync/struct-from-dict")
    def sync_struct_from_dict() -> Item:
        return item_dict(1, "none")

    @api.get("/sync/struct-list-from-tuple")
    def sync_struct_list_from_tuple() -> list[Item]:
        return (item_dict(1, "none"),)


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
        ("/sync/object-list", [{"id": 1, "q": "none", "in_task": False}]),
        ("/sync/struct-from-dict", {"id": 1, "q": "none", "in_task": False}),
        ("/sync/struct-list-from-tuple", [{"id": 1, "q": "none", "in_task": False}]),
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
    ("/sync/struct-from-dict/7", NO_TASK),
    ("/sync/struct-list-from-tuple/7", [NO_TASK]),
    ("/sync/untyped", {"id": 1, "q": "none", "in_task": False}),
    ("/sync/untyped-list", [{"id": 1, "q": "none", "in_task": False}]),
    ("/sync/dict", {"id": 1, "q": "none", "in_task": False}),
    ("/sync/struct", {"id": 1, "q": "none", "in_task": False}),
    ("/sync/struct-list", [{"id": 1, "q": "none", "in_task": False}]),
    ("/sync/object-list", [{"id": 1, "q": "none", "in_task": False}]),
    ("/sync/struct-from-dict", {"id": 1, "q": "none", "in_task": False}),
    ("/sync/struct-list-from-tuple", [{"id": 1, "q": "none", "in_task": False}]),
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


class TitleRow(msgspec.Struct):
    title: str


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("middleware", [[], [PassThrough]], ids=["sync-dispatch", "async-executor"])
def test_a_queryset_from_a_sync_handler_with_a_response_type(middleware):
    """A list type loads the rows. A single Struct type gets the error that names the fix."""
    api = BoltAPI(middleware=middleware)

    @api.get("/rows")
    def rows() -> list[TitleRow]:
        return titles()

    @api.get("/rows/{prefix}")
    def some_rows(prefix: str, *, q: str) -> list[TitleRow]:
        return titles(prefix)

    @api.get("/row")
    def row() -> TitleRow:
        return titles()

    @api.get("/row/{prefix}")
    def some_row(prefix: str, *, q: str) -> TitleRow:
        return titles(prefix)

    Article.objects.create(title="One", content="First", author="A")
    with TestClient(api) as client:
        lists = [client.get(path, params={"q": "bolt"}) for path in ("/rows", "/rows/O")]
        singles = [client.get(path, params={"q": "bolt"}) for path in ("/row", "/row/O")]

    for response in lists:
        assert response.json() == [{"title": "One"}], response.text
    for response in singles:
        assert response.status_code == 500
        errors = response.json()["extra"]["validation_errors"]
        assert errors[0]["msg"].startswith("Response type mismatch: expected a single TitleRow"), errors


def sync_label() -> str:
    return "sync"


async def async_label() -> str:
    await asyncio.sleep(0)
    return "async"


@pytest.mark.django_db(transaction=True)
def test_the_general_executor_passes_every_argument():
    """A handler that blocks, or a sync handler with an async dependency, takes the general executor."""
    api = BoltAPI()

    @api.get("/blocking/{item_id}")
    def blocking(item_id: int, *, q: str):
        return {"count": Article.objects.count(), "item": [item_id, q]}

    @api.get("/blocking-dependency")
    def blocking_dependency(*, q: str, label: str = Depends(sync_label)):
        return {"count": Article.objects.count(), "item": [label, q]}

    @api.get("/async-dependency/{item_id}")
    def async_dependency(item_id: int, *, q: str, label: str = Depends(async_label)):
        return {"item": [item_id, q, label]}

    @api.get("/async-dependency-rows")
    def async_dependency_rows(label: str = Depends(async_label)):
        return titles()

    Article.objects.create(title="One", content="First", author="A")
    with TestClient(api) as client:
        blocking_response = client.get("/blocking/7", params={"q": "bolt"})
        missing = client.get("/blocking/7")
        dependency = client.get("/blocking-dependency", params={"q": "bolt"})
        async_response = client.get("/async-dependency/7", params={"q": "bolt"})
        rows = client.get("/async-dependency-rows")

    assert blocking_response.json() == {"count": 1, "item": [7, "bolt"]}
    # Rust binds no argument, and the injector reports the missing parameter.
    assert missing.status_code == 422, missing.text
    assert dependency.json() == {"count": 1, "item": ["sync", "bolt"]}
    assert async_response.json() == {"item": [7, "bolt", "async"]}
    assert rows.json() == [{"title": "One"}]
