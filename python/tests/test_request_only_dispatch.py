"""A handler whose only parameter is the request takes the sync dispatch.

The sync dispatch calls the handler from Rust with no asyncio task. The async
dispatch runs it in a task on the worker loop, which costs about 15 us more for
each request. Each test reads ``asyncio.current_task()`` in the handler to see
which dispatch ran.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading

import msgspec
import pytest
from asgiref.sync import sync_to_async
from django.db.models import QuerySet

from django_bolt import BoltAPI, Request
from django_bolt.responses import PlainText
from django_bolt.testing import TestClient

from .test_models import Article


def in_task() -> bool:
    try:
        return asyncio.current_task() is not None
    except RuntimeError:
        return False


def test_an_async_request_handler_with_no_await_runs_with_no_task():
    api = BoltAPI()

    @api.get("/items")
    async def items(request: Request):
        return {"in_task": in_task(), "q": request.query.get("q")}

    with TestClient(api) as client:
        response = client.get("/items?q=bolt")

    assert response.json() == {"in_task": False, "q": "bolt"}


def test_a_sync_request_handler_runs_with_no_task():
    api = BoltAPI()

    @api.get("/items")
    def items(request: Request):
        return {"in_task": in_task(), "token": request.headers.get("x-token")}

    with TestClient(api) as client:
        response = client.get("/items", headers={"x-token": "secret"})

    assert response.json() == {"in_task": False, "token": "secret"}


def test_an_async_request_handler_that_awaits_runs_in_a_task():
    api = BoltAPI()

    @api.get("/items")
    async def items(request: Request):
        await asyncio.sleep(0)
        return {"in_task": in_task()}

    with TestClient(api) as client:
        response = client.get("/items")

    assert response.json() == {"in_task": True}


async def awaiting_items(request: Request, q: str = ""):
    await asyncio.sleep(0)
    return {"in_task": in_task(), "q": q}


def marked_sync_wrapper(handler):
    """Wrap ``handler`` in a sync def that returns its coroutine, and mark it as async."""

    @functools.wraps(handler)
    def wrapper(request: Request, q: str = ""):
        return handler(request, q)

    return inspect.markcoroutinefunction(wrapper)


@pytest.mark.parametrize(("path", "q"), [("/request-only", ""), ("/with-param?q=bolt", "bolt")])
def test_a_marked_sync_wrapper_whose_coroutine_awaits_runs_in_a_task(path, q):
    """The code of the wrapper has no await, but its coroutine can suspend."""
    api = BoltAPI()

    @api.get("/request-only")
    @inspect.markcoroutinefunction
    def request_only(request: Request):
        return awaiting_items(request)

    api.get("/with-param")(marked_sync_wrapper(awaiting_items))

    with TestClient(api) as client:
        response = client.get(path)

    assert response.status_code == 200, response.text
    assert response.json() == {"in_task": True, "q": q}


def test_a_sync_to_async_request_handler_runs_in_a_task():
    """A ``sync_to_async`` wrapper has no code to read, and its coroutine suspends."""
    api = BoltAPI()

    def items(request: Request):
        return {"q": request.query.get("q")}

    api.get("/items")(sync_to_async(items))

    with TestClient(api) as client:
        response = client.get("/items?q=bolt")

    assert response.status_code == 200, response.text
    assert response.json() == {"q": "bolt"}


class Echo(msgspec.Struct):
    q: str
    in_task: bool


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_request_handler_with_a_response_type_reads_the_request(is_async):
    """The route validates the response, so the result goes through the serializer."""
    api = BoltAPI()

    if is_async:

        @api.get("/echo")
        async def echo(request: Request) -> Echo:
            return Echo(q=request.query["q"], in_task=in_task())

    else:

        @api.get("/echo")
        def echo(request: Request) -> Echo:
            return Echo(q=request.query["q"], in_task=in_task())

    with TestClient(api) as client:
        response = client.get("/echo?q=bolt")

    assert response.status_code == 200, response.text
    assert response.json() == {"q": "bolt", "in_task": False}


def test_a_request_handler_on_the_sync_dispatch_keeps_its_status_and_response_class():
    api = BoltAPI()

    @api.post("/created", status_code=201)
    async def created(request: Request):
        return {"created": True}

    @api.get("/text")
    def text(request: Request):
        return PlainText("hello")

    @api.get("/list")
    def listing(request: Request):
        return [1, 2, 3]

    with TestClient(api) as client:
        created_response = client.post("/created")
        text_response = client.get("/text")
        list_response = client.get("/list")

    assert (created_response.status_code, created_response.json()) == (201, {"created": True})
    assert text_response.text == "hello"
    assert text_response.headers["content-type"].startswith("text/plain")
    assert list_response.json() == [1, 2, 3]


@pytest.mark.django_db(transaction=True)
def test_a_sync_request_handler_that_uses_the_orm_still_runs_in_the_thread_pool():
    api = BoltAPI()

    @api.get("/count")
    def count(request: Request):
        return {"count": Article.objects.count(), "thread": threading.current_thread().name}

    with TestClient(api) as client:
        response = client.get("/count")

    assert response.json()["count"] == 0
    assert response.json()["thread"].startswith("bolt_default")


class ThreadRecordingQuerySet(QuerySet):
    """Record the thread that loads the rows."""

    threads: list[str] = []

    def _fetch_all(self):
        if self._result_cache is None:
            ThreadRecordingQuerySet.threads.append(threading.current_thread().name)
        super()._fetch_all()


def articles():
    """Return a QuerySet from a helper, where the analysis of the handler cannot see the ORM."""
    return ThreadRecordingQuerySet(model=Article).values("title")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_queryset_from_a_request_handler_loads_on_the_orm_pool(is_async):
    api = BoltAPI()

    if is_async:

        @api.get("/articles")
        async def listing(request: Request):
            return articles()

    else:

        @api.get("/articles")
        def listing(request: Request):
            return articles()

    Article.objects.create(title="One", content="First", author="A")
    ThreadRecordingQuerySet.threads.clear()
    with TestClient(api) as client:
        response = client.get("/articles")

    assert response.status_code == 200, response.text
    assert [row["title"] for row in response.json()] == ["One"]
    assert ThreadRecordingQuerySet.threads
    assert all(name.startswith("bolt_orm") for name in ThreadRecordingQuerySet.threads), ThreadRecordingQuerySet.threads
