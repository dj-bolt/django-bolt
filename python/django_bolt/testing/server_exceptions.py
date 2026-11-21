"""Bring the exception of a handler back to the test, for ``raise_server_exceptions=True``.

A handler runs on a test worker thread, and the test gets only the response.
Thus the dispatch of each route of the test client goes through a wrapper:

- The error path of ``BoltAPI`` keeps the exception of a server error in
  ``request.state``. The wrapper then gives a response with a token header.
- The wrapper also records an exception that leaves the dispatch.
- For a streaming response, the wrapper records an exception of the body.

The transport reads the token header, removes it, and raises the exception.
Only the test client uses this module, so ``runbolt`` pays nothing for it.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from django_bolt import BoltAPI
from django_bolt._bridge import make_bound_dispatch
from django_bolt.api import SERVER_EXCEPTION_STATE_KEY
from django_bolt.serialization import _BODY_STREAM

# The transport removes this header. The test never sees it.
TOKEN_HEADER = "x-django-bolt-test-exception"


class ServerExceptions:
    """The exceptions of the requests of one test client, by token."""

    __slots__ = ("_held", "_tokens")

    def __init__(self) -> None:
        self._held: dict[str, list[BaseException]] = {}
        self._tokens = itertools.count(1)

    def _hold(self) -> tuple[str, list[BaseException]]:
        token = str(next(self._tokens))
        held: list[BaseException] = []
        self._held[token] = held
        return token, held

    def take(self, headers: list[tuple[str, bytes]]) -> BaseException | None:
        """Remove the token header from ``headers``, and return the exception of its request."""
        for index, (name, value) in enumerate(headers):
            if name == TOKEN_HEADER:
                del headers[index]
                held = self._held.pop(value.decode("ascii"))
                return held[0] if held else None
        return None

    def error_wire(self, error: BaseException) -> tuple:
        """Return a response that tells the transport to raise ``error``."""
        token, held = self._hold()
        held.append(error)
        return 500, ("plaintext", "text/plain", [(TOKEN_HEADER, token)], None), 0, b""

    def check(self, request: Any, wire: Any) -> Any:
        """Return the wire of a dispatch, or a response that raises the exception of the request."""
        if isinstance(wire, bytes):
            # The fast path of a sync handler. Its handler did not raise.
            return wire
        error = request.state.pop(SERVER_EXCEPTION_STATE_KEY, None)
        if error is not None:
            return self.error_wire(error)
        if wire[2] == _BODY_STREAM and isinstance(wire[1], tuple):
            return self._watch_stream(wire)
        return wire

    def _watch_stream(self, wire: tuple) -> tuple:
        status, (response_type, content_type, headers, cookies), body_kind, stream = wire
        content = stream.content
        token, held = self._hold()
        if stream.is_async_generator:
            stream.content = _record_async(content, held)
        elif hasattr(content, "__iter__"):
            stream.content = _record_sync(content, held)
        headers = [*(headers or ()), (TOKEN_HEADER, token)]
        return status, (response_type, content_type, headers, cookies), body_kind, stream


def _record_sync(content: Any, held: list[BaseException]) -> Iterator[Any]:
    """Yield the chunks of ``content``, and record the exception that ends it."""
    try:
        yield from content
    except Exception as error:
        held.append(error)
        raise


async def _record_async(content: Any, held: list[BaseException]) -> AsyncIterator[Any]:
    """Yield the chunks of ``content``, and record the exception that ends it."""
    iterator = content.__aiter__() if hasattr(content, "__aiter__") else content
    try:
        while True:
            try:
                item = await iterator.__anext__()
            except StopAsyncIteration:
                return
            yield item
    except Exception as error:
        held.append(error)
        raise
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


def bound_dispatches(
    api: BoltAPI, handler: Callable, handler_id: int, exceptions: ServerExceptions | None
) -> tuple[Callable, Callable]:
    """Return the async and the sync dispatch of one route of a test client.

    Without ``exceptions``, they are the dispatches of ``runbolt``.
    """
    dispatch = make_bound_dispatch(api._dispatch, handler, handler_id)
    dispatch_sync = make_bound_dispatch(api._dispatch_sync, handler, handler_id)
    if exceptions is None:
        return dispatch, dispatch_sync

    async def checked_dispatch(request: Any) -> Any:
        try:
            wire = await dispatch(request)
        except Exception as error:
            return exceptions.error_wire(error)
        return exceptions.check(request, wire)

    def checked_dispatch_sync(request: Any) -> Any:
        try:
            result = dispatch_sync(request)
        except Exception as error:
            return exceptions.error_wire(error)
        if callable(result):
            # Rust awaits the call on the worker loop, for example to load a QuerySet.
            async def checked_result(request: Any) -> Any:
                try:
                    wire = await result(request)
                except Exception as error:
                    return exceptions.error_wire(error)
                return exceptions.check(request, wire)

            return checked_result
        return exceptions.check(request, result)

    return checked_dispatch, checked_dispatch_sync
