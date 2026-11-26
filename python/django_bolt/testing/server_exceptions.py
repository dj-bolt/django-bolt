"""Bring the exception of a handler back to the test, for ``raise_server_exceptions=True``.

A handler runs on a test worker thread, and the test gets only the response.
Thus the dispatch of each route of the test client goes through a wrapper:

- The wrapper puts a key in ``request.state``. With the key, the error path of
  ``BoltAPI`` keeps the exception of a server error there. The wrapper removes
  the key and gives a response with a token header when the status is 5xx.
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

_MIN_SERVER_ERROR_STATUS = 500


class ServerExceptions:
    """The exceptions of the requests of one test client, by token."""

    __slots__ = ("_held", "_tokens")

    def __init__(self) -> None:
        # Only a failed request has an entry. The transport removes it.
        self._held: dict[str, BaseException] = {}
        self._tokens = itertools.count(1)

    def take(self, headers: list[tuple[str, bytes]]) -> BaseException | None:
        """Remove the token header from ``headers``, and return the exception of its request."""
        for index, (name, value) in enumerate(headers):
            if name == TOKEN_HEADER:
                del headers[index]
                return self._held.pop(value.decode("ascii"), None)
        return None

    def error_wire(self, error: BaseException) -> tuple:
        """Return a response that tells the transport to raise ``error``."""
        token = str(next(self._tokens))
        self._held[token] = error
        return 500, ("plaintext", "text/plain", [(TOKEN_HEADER, token)], None), 0, b""

    def check(self, wire: Any, error: BaseException | None) -> Any:
        """Return the wire of a dispatch, or a response that raises ``error`` for a 5xx."""
        if isinstance(wire, bytes):
            # The fast path of a sync handler. Its handler did not raise.
            return wire
        # An error_formatter can answer the exception with a 4xx. Then the test gets the response.
        if error is not None and wire[0] >= _MIN_SERVER_ERROR_STATUS:
            return self.error_wire(error)
        if wire[2] == _BODY_STREAM and isinstance(wire[1], tuple):
            return self._watch_stream(wire)
        return wire

    def _watch_stream(self, wire: tuple) -> tuple:
        status, (response_type, content_type, headers, cookies), body_kind, stream = wire
        content = stream.content
        token = str(next(self._tokens))
        if stream.is_async_generator:
            stream.content = _record_async(content, self._held, token)
        elif hasattr(content, "__next__") or hasattr(content, "__iter__"):
            stream.content = _record_sync(content, self._held, token)
        headers = [*(headers or ()), (TOKEN_HEADER, token)]
        return status, (response_type, content_type, headers, cookies), body_kind, stream


def _record_sync(content: Any, held: dict[str, BaseException], token: str) -> Iterator[Any]:
    """Yield the chunks of ``content``, and record the exception that ends it.

    It takes and closes the iterator as the Rust stream does: ``content`` itself
    when it has ``__next__``, else ``iter(content)``.
    """
    iterator = None
    try:
        iterator = content if hasattr(content, "__next__") else iter(content)
        while True:
            try:
                # As Bolt does: an instance can hold __next__, and next() looks only on the class.
                item = iterator.__next__()
            except StopIteration:
                return
            yield item
    except Exception as error:
        held[token] = error
        raise
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


async def _record_async(content: Any, held: dict[str, BaseException], token: str) -> AsyncIterator[Any]:
    """Yield the chunks of ``content``, and record the exception that ends it."""
    iterator = None
    try:
        iterator = content.__aiter__() if hasattr(content, "__aiter__") else content
        while True:
            try:
                item = await iterator.__anext__()
            except StopAsyncIteration:
                return
            yield item
    except Exception as error:
        held[token] = error
        raise
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


def bound_dispatches(
    api: BoltAPI,
    handler: Callable,
    handler_id: int,
    exceptions: ServerExceptions | None,
    script_prefix: str | None = None,
) -> tuple[Callable, Callable]:
    """Return the async and the sync dispatch of one route of a test client.

    Without ``exceptions``, they are the dispatches of ``runbolt``.
    """
    dispatch = make_bound_dispatch(api._dispatch, handler, handler_id, script_prefix)
    dispatch_sync = make_bound_dispatch(api._dispatch_sync, handler, handler_id, script_prefix)
    if exceptions is None:
        return dispatch, dispatch_sync

    # A middleware can clear request.state, so each pop has a default.
    async def checked_dispatch(request: Any) -> Any:
        state = request.state
        state[SERVER_EXCEPTION_STATE_KEY] = None
        try:
            wire = await dispatch(request)
        except Exception as error:
            return exceptions.error_wire(error)
        finally:
            # The exception holds the request in its traceback. The state must not hold it.
            kept = state.pop(SERVER_EXCEPTION_STATE_KEY, None)
        return exceptions.check(wire, kept)

    def checked_dispatch_sync(request: Any) -> Any:
        state = request.state
        state[SERVER_EXCEPTION_STATE_KEY] = None
        try:
            result = dispatch_sync(request)
        except Exception as error:
            state.pop(SERVER_EXCEPTION_STATE_KEY, None)
            return exceptions.error_wire(error)
        if callable(result):
            # Rust awaits the call on the worker loop, for example to load a QuerySet.
            async def checked_result(request: Any) -> Any:
                try:
                    wire = await result(request)
                except Exception as error:
                    return exceptions.error_wire(error)
                finally:
                    kept = state.pop(SERVER_EXCEPTION_STATE_KEY, None)
                return exceptions.check(wire, kept)

            return checked_result
        return exceptions.check(result, state.pop(SERVER_EXCEPTION_STATE_KEY, None))

    return checked_dispatch, checked_dispatch_sync
