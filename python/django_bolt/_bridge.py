"""Per-route dispatch binding for the Rust->Python call boundary.

Async HTTP dispatch runs on the process-lived worker loop facade in
``_worker_loop.py``; this module holds the registration-time helper that
produces the single-argument dispatch callable Rust stores per route.
"""

from __future__ import annotations

import inspect

from django.urls import clear_script_prefix, set_script_prefix

from . import concurrency


def make_bound_dispatch(dispatch, handler, handler_id):
    """Create the per-route single-argument dispatch callable stored in Rust.

    A plain closure, NOT functools.partial with a keyword: partial's stored
    kwargs force a dict copy on every call (~279ns vs ~101ns measured for the
    closure). The hot Rust->Python call then passes only (request,).

    With ``FORCE_SCRIPT_NAME``, the closure sets the script prefix for the
    request, as Django's handlers do. ``reverse()`` then puts the script name
    in front. Without the setting, the closure is the same as before.
    """
    script_prefix = concurrency._script_prefix
    if script_prefix is None:

        def bound_dispatch(request):
            return dispatch(handler, request, handler_id)

        return bound_dispatch

    if inspect.iscoroutinefunction(dispatch):
        # The task of the request runs in its own copy of the context, so the
        # prefix does not stay on the worker thread after the request.
        async def bound_async_dispatch(request):
            set_script_prefix(script_prefix)
            return await dispatch(handler, request, handler_id)

        return bound_async_dispatch

    def bound_sync_dispatch(request):
        set_script_prefix(script_prefix)
        try:
            return dispatch(handler, request, handler_id)
        finally:
            # The worker thread serves the next request with no prefix of its own.
            clear_script_prefix()

    return bound_sync_dispatch
