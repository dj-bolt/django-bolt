"""Per-route dispatch binding for the Rust->Python call boundary.

Async HTTP dispatch runs on the process-lived worker loop facade in
``_worker_loop.py``; this module holds the registration-time helper that
produces the single-argument dispatch callable Rust stores per route.
"""

from __future__ import annotations


def make_bound_dispatch(dispatch, handler, handler_id):
    """Create the per-route single-argument dispatch callable stored in Rust.

    A plain closure, NOT functools.partial with a keyword: partial's stored
    kwargs force a dict copy on every call (~279ns vs ~101ns measured for the
    closure). The hot Rust->Python call then passes only (request,).
    """

    def bound_dispatch(request):
        return dispatch(handler, request, handler_id)

    return bound_dispatch
