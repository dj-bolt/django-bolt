"""``TestClient(raise_server_exceptions=True)`` raises the exception of a handler (issue #869).

Starlette's test client does the same. The test then shows the traceback at the
place of the fault, not only a 500. With ``raise_server_exceptions=False``, the
test gets the 500 response that a real server sends.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import weakref

import msgspec
import pytest
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import HttpResponse

from django_bolt import BoltAPI
from django_bolt.error_handlers import problem_details
from django_bolt.exceptions import HTTPException, ResponseValidationError
from django_bolt.middleware import DjangoMiddlewareStack
from django_bolt.responses import JSON, StreamingResponse
from django_bolt.testing import AsyncTestClient, TestClient

from .test_models import Article


class Boom(Exception):
    """The exception that the handlers of these tests raise."""


class PassThroughMiddleware:
    """A Django middleware that does nothing. Its route runs on a request lane."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


class HandlingMiddleware(PassThroughMiddleware):
    """A Django middleware whose ``process_exception`` gives a response."""

    def process_exception(self, request, exception):
        return HttpResponse("handled", status=418)


def make_api(**kwargs) -> BoltAPI:
    api = BoltAPI(**kwargs)

    @api.get("/sync")
    def sync_boom():
        raise Boom("sync")

    @api.get("/trivial-async")
    async def trivial_async_boom():
        raise Boom("trivial async")

    @api.get("/async")
    async def async_boom():
        await asyncio.sleep(0)
        raise Boom("async")

    @api.get("/ok")
    def ok():
        return {"ok": True}

    return api


ROUTES = [("/sync", "sync"), ("/trivial-async", "trivial async"), ("/async", "async")]
PATHS = [path for path, _ in ROUTES]


# The option is on by default.
@pytest.mark.parametrize("client_kwargs", [{}, {"raise_server_exceptions": True}])
@pytest.mark.parametrize(("path", "message"), ROUTES)
def test_the_client_raises_the_exception_of_the_handler(path, message, client_kwargs):
    with TestClient(make_api(), **client_kwargs) as client:
        with pytest.raises(Boom, match=f"^{message}$"):
            client.get(path)
        # The client still works after the exception.
        assert client.get("/ok").json() == {"ok": True}


@pytest.mark.parametrize("path", PATHS)
def test_without_the_option_the_client_gets_the_500(path):
    with TestClient(make_api(), raise_server_exceptions=False) as client:
        response = client.get(path)
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}


@pytest.mark.parametrize(("path", "message"), ROUTES)
def test_a_route_with_django_middleware_raises_the_exception(path, message):
    api = make_api(middleware=[DjangoMiddlewareStack([PassThroughMiddleware])])
    with TestClient(api) as client:
        with pytest.raises(Boom, match=f"^{message}$"):
            client.get(path)
        assert client.get("/ok").json() == {"ok": True}


@pytest.mark.parametrize("path", PATHS)
def test_a_route_with_django_middleware_gives_the_500_without_the_option(path):
    api = make_api(middleware=[DjangoMiddlewareStack([PassThroughMiddleware])])
    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get(path)
    assert response.status_code == 500


@pytest.mark.parametrize("path", PATHS)
def test_an_exception_that_process_exception_handles_is_not_raised(path):
    api = make_api(middleware=[DjangoMiddlewareStack([HandlingMiddleware])])
    with TestClient(api) as client:
        response = client.get(path)
    assert response.status_code == 418
    assert response.content == b"handled"


@pytest.mark.parametrize(("path", "message"), ROUTES)
def test_the_async_client_raises_the_exception_of_the_handler(path, message):
    async def run():
        async with AsyncTestClient(make_api()) as client:
            with pytest.raises(Boom, match=f"^{message}$"):
                await client.get(path)
            assert (await client.get("/ok")).json() == {"ok": True}

    asyncio.run(run())


def test_the_async_client_gives_the_500_without_the_option():
    async def run():
        async with AsyncTestClient(make_api(), raise_server_exceptions=False) as client:
            return await client.get("/async")

    assert asyncio.run(run()).status_code == 500


def test_concurrent_requests_raise_only_their_own_exception():
    api = BoltAPI()

    @api.get("/slow-ok")
    async def slow_ok():
        await asyncio.sleep(0.05)
        return {"ok": True}

    @api.get("/fail")
    async def fail():
        await asyncio.sleep(0.01)
        raise Boom("fail")

    async def run():
        async with AsyncTestClient(api) as client:
            return await asyncio.gather(
                client.get("/slow-ok"), client.get("/fail"), client.get("/slow-ok"), return_exceptions=True
            )

    first, failed, last = asyncio.run(run())
    assert first.json() == {"ok": True}
    assert isinstance(failed, Boom)
    assert last.json() == {"ok": True}


def test_an_error_response_of_the_api_is_not_raised():
    """An exception that Bolt answers with a 4xx, or an HTTPException, is a response, not a fault."""
    api = BoltAPI()

    @api.get("/http-500")
    def http_500():
        raise HTTPException(status_code=500, detail="planned")

    @api.get("/denied")
    async def denied():
        raise PermissionDenied

    @api.get("/missing/{item_id}")
    def missing(item_id: int):
        return {"item_id": item_id}

    with TestClient(api) as client:
        assert client.get("/http-500").status_code == 500
        assert client.get("/denied").status_code == 403
        assert client.get("/missing/abc").status_code == 422
        assert client.get("/not-a-route").status_code == 404


def test_an_error_formatter_does_not_stop_the_raise():
    """An error_formatter that answers with a 5xx formats the 500, and the test still gets the exception."""
    api = make_api(error_formatter=problem_details)
    with TestClient(api) as client, pytest.raises(Boom, match="^sync$"):
        client.get("/sync")
    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get("/sync")
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"


class Conflict(Exception):
    """An exception that ``conflict_formatter`` answers with a 409."""


def conflict_formatter(error, request):
    if isinstance(error.exception, Conflict):
        return JSON({"code": "conflict"}, status_code=409)
    return JSON({"code": error.kind}, status_code=error.status)


def test_an_exception_that_the_error_formatter_answers_with_a_4xx_is_not_raised():
    api = BoltAPI(error_formatter=conflict_formatter)

    @api.get("/sync")
    def sync_conflict():
        raise Conflict("sync")

    @api.get("/async")
    async def async_conflict():
        await asyncio.sleep(0)
        raise Conflict("async")

    with TestClient(api) as client:
        for path in ("/sync", "/async"):
            response = client.get(path)
            assert response.status_code == 409, path
            assert response.json() == {"code": "conflict"}, path


class Item(msgspec.Struct):
    name: str


def test_a_response_validation_error_is_raised():
    api = BoltAPI()

    @api.get("/item")
    async def bad_item() -> Item:
        return {"title": "no name"}

    with TestClient(api) as client, pytest.raises(ResponseValidationError):
        client.get("/item")


class BrokenArticles(QuerySet):
    def _fetch_all(self):
        raise Boom("rows")


def broken_articles() -> QuerySet:
    return BrokenArticles(model=Article)


def test_the_exception_of_a_queryset_that_loads_after_a_sync_dispatch_is_raised():
    """The sync dispatch defers the QuerySet of a helper, and Rust loads it on the ORM pool."""
    api = BoltAPI()

    @api.get("/articles")
    def articles(request):
        return broken_articles()

    with TestClient(api) as client, pytest.raises(Boom, match="^rows$"):
        client.get("/articles")


def test_a_client_without_the_option_keeps_no_exception(caplog):
    """A client with the option does not make a later client keep the exception in the request."""
    # A log record holds the exception. This test looks only at the request.
    for logger in ("django.server", "django_bolt"):
        caplog.set_level(logging.CRITICAL, logger=logger)
    raised = []
    api = BoltAPI()

    @api.get("/boom")
    def boom():
        error = Boom("boom")
        raised.append(weakref.ref(error))
        raise error

    with TestClient(api) as client, pytest.raises(Boom):
        client.get("/boom")
    with TestClient(api, raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == 500
    gc.collect()
    # The exception holds the request in its traceback. Thus the request must not hold it.
    assert [ref() for ref in raised] == [None, None]


def sync_chunks():
    yield b"first,"
    raise Boom("sync stream")


async def async_chunks():
    yield b"first,"
    await asyncio.sleep(0)
    raise Boom("async stream")


class NextOnlyChunks:
    """A stream with ``__next__`` and no ``__iter__``. Bolt reads such an object directly."""

    def __init__(self) -> None:
        self.sent = False

    def __next__(self) -> bytes:
        if self.sent:
            raise Boom("next-only stream")
        self.sent = True
        return b"first,"


class FailingAsyncIterable:
    def __aiter__(self):
        raise Boom("aiter stream")


class ClosableChunks:
    """A stream that frees a resource in ``close()``."""

    def __init__(self) -> None:
        self.items = iter([b"a,", b"b"])
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return next(self.items)

    def close(self) -> None:
        self.closed = True


def make_stream_api() -> BoltAPI:
    api = BoltAPI()

    @api.get("/next-only-stream")
    def next_only_stream():
        return StreamingResponse(NextOnlyChunks(), media_type="text/plain")

    @api.get("/aiter-stream")
    async def aiter_stream():
        return StreamingResponse(FailingAsyncIterable(), media_type="text/plain")

    @api.get("/sync-stream")
    def sync_stream():
        return StreamingResponse(sync_chunks(), media_type="text/plain")

    @api.get("/async-stream")
    async def async_stream():
        return StreamingResponse(async_chunks(), media_type="text/plain")

    @api.get("/good-stream")
    async def good_stream():
        async def chunks():
            yield b"a,"
            yield b"b"

        return StreamingResponse(chunks(), media_type="text/plain", headers={"x-kept": "yes"})

    return api


STREAMS = [
    ("/sync-stream", "sync stream"),
    ("/async-stream", "async stream"),
    ("/next-only-stream", "next-only stream"),
]


@pytest.mark.parametrize(("path", "message"), [*STREAMS, ("/aiter-stream", "aiter stream")])
def test_an_exception_of_a_streaming_body_is_raised(path, message, caplog):
    with TestClient(make_stream_api()) as client, pytest.raises(Boom, match=f"^{message}$"):
        client.get(path)
    # The server still logs the error, as runbolt does.
    assert any(record.name == "django_bolt.streaming" for record in caplog.records)


@pytest.mark.parametrize(("path", "message"), STREAMS)
def test_without_the_option_a_streaming_body_ends_at_the_exception(path, message):
    with TestClient(make_stream_api(), raise_server_exceptions=False) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.content == b"first,"


def test_a_good_stream_has_its_body_and_headers_only():
    with TestClient(make_stream_api()) as client:
        response = client.get("/good-stream")
    assert response.content == b"a,b"
    assert response.headers["x-kept"] == "yes"
    assert not [name for name in response.headers if name.startswith("x-django-bolt")]


def test_the_client_closes_the_stream_after_its_last_chunk():
    """The wrapper of the client closes the stream, as Bolt does without it."""
    api = BoltAPI()
    chunks = ClosableChunks()

    @api.get("/closable-stream")
    def closable_stream():
        return StreamingResponse(chunks, media_type="text/plain")

    with TestClient(api) as client:
        response = client.get("/closable-stream")

    assert response.content == b"a,b"
    assert chunks.closed
