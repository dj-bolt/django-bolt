"""Loop-thread dispatch bridge: eager coroutine execution for async handlers.

Rust schedules ``eager_dispatch`` on the asyncio loop thread via
``call_soon_threadsafe`` instead of wrapping every dispatch coroutine in an
asyncio Task (``pyo3_async_runtimes::into_future_with_locals`` →
``ensure_future``). Measured on the dispatch probes (docs/PROFILING.md),
``ensure_future`` + Task bookkeeping was ~50% of all GIL time under async
load — while an actual suspend/resume cost almost nothing.

Eager semantics:

- The coroutine's first segment runs synchronously right here, ON the loop
  thread (so ``asyncio.get_running_loop()``, ``create_task``, dependency
  ``gather`` all work exactly as inside a Task's first step).
- If it finishes without ever suspending — the common case for handlers whose
  awaits complete immediately (cached deps, no I/O this request) — the result
  goes straight back to Rust: no Task, no extra loop cycle.
- On the first real suspension the remainder is handed to a real asyncio Task
  (`_drive_started`), preserving cancellation, exception, and contextvars
  semantics from that point on.

Known, deliberate difference vs a Task-per-request: ``asyncio.current_task()``
returns ``None`` inside the FIRST segment (before the first real suspension).
After the first suspension a real Task exists. Set
``DJANGO_BOLT_EAGER_DISPATCH=0`` to restore the previous Task-per-request
bridge if a library depends on that.
"""

from __future__ import annotations

import asyncio


async def _drive_started(coro, pending):
    """Drive an already-started dispatch coroutine to completion.

    ``coro`` was advanced by ``eager_dispatch`` and is suspended having yielded
    ``pending``. We await each yielded object inside this (real) Task and step
    the coroutine manually — the coroutine fetches results from the awaited
    future itself on resume (``Future.__await__`` returns ``self.result()``),
    so ``send(None)`` / ``throw(exc)`` is exactly what ``Task.__step`` does.

    A yielded ``None`` is a bare reschedule request (e.g. ``asyncio.sleep(0)``);
    ``asyncio.sleep(0)`` reproduces Task's call_soon behavior for it.
    """
    while True:
        try:
            if pending is None:
                await asyncio.sleep(0)
            else:
                # Task.__step resets this flag when it consumes a yielded
                # future; we consumed the yield manually, so we must too —
                # FutureIter raises "await wasn't used with future" on a
                # pending future whose blocking flag is still set.
                if getattr(pending, "_asyncio_future_blocking", False):
                    pending._asyncio_future_blocking = False
                await pending
        except BaseException as exc:  # delivered INTO the handler coroutine
            try:
                pending = coro.throw(exc)
            except StopIteration as stop:
                return stop.value
        else:
            try:
                pending = coro.send(None)
            except StopIteration as stop:
                return stop.value


def _resolve_task(task, resolver):
    try:
        resolver.set_result(task.result())
    except BaseException as exc:  # noqa: BLE001 — resolver carries it to Rust's error path
        resolver.set_exception(exc)


def eager_dispatch(dispatch, request, resolver):
    """Entry point scheduled on the loop thread by Rust (call_soon_threadsafe).

    ``dispatch`` is the per-route bound callable (partial binding handler and
    handler_id) stored in the Rust Route — a single-argument call. Creates the
    dispatch coroutine and eagerly runs its first segment. Resolver is a
    Rust-backed object with set_result/set_exception that completes the
    tokio-side response future.
    """
    try:
        coro = dispatch(request)
        try:
            pending = coro.send(None)
        except StopIteration as stop:
            resolver.set_result(stop.value)
            return
        task = asyncio.ensure_future(_drive_started(coro, pending))
    except BaseException as exc:  # noqa: BLE001 — resolver carries it to Rust's error path
        resolver.set_exception(exc)
        return
    task.add_done_callback(lambda t: _resolve_task(t, resolver))


def make_bound_dispatch(dispatch, handler, handler_id):
    """Create the per-route single-argument dispatch callable stored in Rust.

    A plain closure, NOT functools.partial with a keyword: partial's stored
    kwargs force a dict copy on every call (~279ns vs ~101ns measured for the
    closure). The hot Rust->Python call then passes only (request,).
    """

    def bound_dispatch(request):
        return dispatch(handler, request, handler_id)

    return bound_dispatch
