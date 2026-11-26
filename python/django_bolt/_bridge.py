"""Per-route dispatch binding for the Rust->Python call boundary.

Async HTTP dispatch runs on the process-lived worker loop facade in
``_worker_loop.py``; this module holds the registration-time helper that
produces the single-argument dispatch callable Rust stores per route.
"""

from __future__ import annotations

import inspect
from contextvars import copy_context
from functools import partial

from django.urls import set_script_prefix


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
            return await dispatch(handler, request, handler_id)

        return bound_async_dispatch

    def dispatch_with_script_prefix(request):
        set_script_prefix(script_prefix)
        result = dispatch(handler, request, handler_id)
        if callable(result):
            # A sync executor defers a QuerySet. Rust runs the call in a task
            # on the worker loop, out of this context.
            return partial(_deferred_dispatch, script_prefix, result)
        return result

    def bound_sync_dispatch(request):
        # A copy of the context ends with the request, so the prefix does not
        # stay on the worker thread.
        return copy_context().run(dispatch_with_script_prefix, request)

    return bound_sync_dispatch


async def _deferred_dispatch(script_prefix, deferred, request):
    set_script_prefix(script_prefix)
    return await deferred(request)
