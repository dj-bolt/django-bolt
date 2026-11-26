"""Per-route dispatch binding for the Rust->Python call boundary.

Async HTTP dispatch runs on the process-lived worker loop facade in
``_worker_loop.py``; this module holds the registration-time helper that
produces the single-argument dispatch callable Rust stores per route.
"""

from __future__ import annotations

import inspect
from functools import partial

from django.urls import get_script_prefix, set_script_prefix

from ._script_prefix import restore, stream_body
from .serialization import _BODY_STREAM


def make_bound_dispatch(dispatch, handler, handler_id, script_prefix=None):
    """Create the per-route single-argument dispatch callable stored in Rust.

    A plain closure, NOT functools.partial with a keyword: partial's stored
    kwargs force a dict copy on every call (~279ns vs ~101ns measured for the
    closure). The hot Rust->Python call then passes only (request,).

    With a ``script_prefix`` (``FORCE_SCRIPT_NAME``), the closure gives the
    request that prefix, as Django's handlers do. See ``_script_prefix``.
    """
    if script_prefix is None:

        def bound_dispatch(request):
            return dispatch(handler, request, handler_id)

        return bound_dispatch

    if inspect.iscoroutinefunction(dispatch):
        # The task of the request has a context of its own.
        async def bound_async_dispatch(request):
            set_script_prefix(script_prefix)
            return _with_stream_prefix(await dispatch(handler, request, handler_id), script_prefix)

        return bound_async_dispatch

    def bound_sync_dispatch(request):
        # A sync dispatch does not suspend. The prefix is on this thread for
        # the call only, and each other value of the thread stays.
        previous = get_script_prefix()
        set_script_prefix(script_prefix)
        try:
            result = dispatch(handler, request, handler_id)
        finally:
            restore(previous)
        if callable(result):
            # A sync executor defers a QuerySet. Rust runs the call in a task
            # on the worker loop, after this call.
            return partial(_deferred_dispatch, script_prefix, result)
        return _with_stream_prefix(result, script_prefix)

    return bound_sync_dispatch


async def _deferred_dispatch(script_prefix, deferred, request):
    set_script_prefix(script_prefix)
    return await deferred(request)


def _with_stream_prefix(result, script_prefix):
    """Give the body of a streaming response the script prefix. Rust iterates it after the dispatch."""
    if type(result) is tuple and result[2] == _BODY_STREAM:
        stream = result[3]
        stream.content = stream_body(stream.content, stream.is_async_generator, script_prefix)
    return result
