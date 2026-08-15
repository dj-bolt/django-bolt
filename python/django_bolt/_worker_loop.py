"""Process-lived asyncio loop used by Bolt's worker-local HTTP dispatch.

The loop keeps one identity for the life of the process.  It is a real
``asyncio.SelectorEventLoop`` subclass, so transports, ``sock_*`` helpers,
DNS, pipes, TLS, and subprocesses come from the stdlib; the ready queue,
timers, signals, and file-descriptor readiness are driven by Rust/Tokio
(see ``src/worker_loop.rs`` and ``src/worker_fd.rs``).

Callbacks may resume on a different OS thread after a suspension (the Tokio
pump task can migrate between worker threads).  Contextvars are preserved via
Handle contexts; ``threading.local`` state is not carried across awaits.

Known deviation from stdlib loops: ``remove_signal_handler`` stops delivering
the signal to Python but cannot restore the default disposition — Tokio's
process-level signal handler stays installed for the process lifetime, so a
"removed" signal is effectively ignored rather than reset to ``SIG_DFL``.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import selectors

from .concurrency import _get_default_executor


class _NoSelector(selectors.BaseSelector):
    """Placeholder for the stdlib selector slot; the Rust reactor owns fd readiness."""

    def register(self, fileobj, events, data=None):
        raise NotImplementedError("WorkerLoop watches descriptors natively")

    def unregister(self, fileobj):
        raise NotImplementedError("WorkerLoop watches descriptors natively")

    def select(self, timeout=None):
        raise NotImplementedError("WorkerLoop is driven by django-bolt")

    def get_map(self):
        return {}


class WorkerLoop(asyncio.SelectorEventLoop):
    """An asyncio loop whose ready queue and reactor are serviced by Tokio.

    There is one instance per process, so tasks may outlive an HTTP response
    and loop-bound objects can safely be reused by later requests.  The stdlib
    selector loop supplies transports, ``sock_*`` helpers, DNS, pipes, TLS,
    and subprocesses; this subclass replaces the four primitives they are all
    built on (``_add_reader``/``_add_writer``/``_remove_reader``/
    ``_remove_writer``) with Rust-side descriptor watchers, and the ready
    queue and timers with the Rust pump.
    """

    def __init__(self, scheduler):
        # Base __init__ may schedule callbacks (set_debug), so the pump
        # handle must exist first.
        self._scheduler = scheduler
        super().__init__(selector=_NoSelector())
        self._default_executor = _get_default_executor()
        # fd -> Handle, mirroring the stdlib selector's (reader, writer) data.
        self._readers = {}
        self._writers = {}

    def _make_self_pipe(self):
        # Rust wakes the pump; there is no thread to interrupt with a pipe.
        self._ssock = None
        self._csock = None

    def _close_self_pipe(self):
        return None

    def _write_to_self(self):
        return None

    def __del__(self):
        # Process-lived: never warn or close from the finalizer.
        return None

    def is_running(self):
        return not self._closed

    def close(self):
        raise RuntimeError("WorkerLoop is process-managed and cannot be closed")

    def run_forever(self):
        raise RuntimeError("WorkerLoop is already driven by django-bolt")

    def run_until_complete(self, future):
        raise RuntimeError("WorkerLoop is already running; await the future instead")

    def stop(self):
        raise RuntimeError("WorkerLoop is process-managed and cannot be stopped")

    async def shutdown_asyncgens(self):
        # Async generators are finalized with their owning Tasks; the
        # process-lived loop itself is shut down with the worker process.
        return None

    async def shutdown_default_executor(self, timeout=None):
        # The default pool is process-owned and shared with framework helpers.
        # Per-request code must not tear it down.
        return None

    def call_soon(self, callback, *args, context=None):
        self._check_closed()
        # Stdlib loops reject coroutines unconditionally; this loop only pays
        # for the check in debug mode because call_soon is on the per-wakeup
        # hot path (a queued coroutine still fails, just later and noisier).
        if self._debug and (asyncio.iscoroutine(callback) or inspect.iscoroutinefunction(callback)):
            raise TypeError(f"coroutines cannot be used with call_soon(): {callback!r}")
        handle = asyncio.Handle(callback, args, self, context=context)
        self._scheduler.call_soon(handle)
        return handle

    def call_soon_threadsafe(self, callback, *args, context=None):
        return self.call_soon(callback, *args, context=context)

    def call_at(self, when, callback, *args, context=None):
        self._check_closed()
        handle = asyncio.TimerHandle(when, callback, args, self, context=context)
        delay = when - self.time()
        if delay <= 0.0:
            self._scheduler.call_soon(handle)
        else:
            self._scheduler.call_later(delay, handle)
        return handle

    def call_later(self, delay, callback, *args, context=None):
        return self.call_at(self.time() + delay, callback, *args, context=context)

    def _timer_handle_cancelled(self, handle):
        # Rust re-checks the cancelled bit at fire time; this notice lets the
        # timer thread compact cancelled handles out of its heap early instead
        # of retaining them (and their captured arguments) until the deadline.
        self._scheduler.timer_cancelled()

    def create_task(self, coro, *, name=None, context=None, **kwargs):
        self._check_closed()
        if self._task_factory is not None:
            if context is None:
                task = self._task_factory(self, coro)
            else:
                task = self._task_factory(self, coro, context=context)
            if task is None:
                raise TypeError("task factory returned None")
            if name is not None:
                task.set_name(name)
            return task
        return asyncio.Task(coro, loop=self, name=name, context=context, **kwargs)

    # Descriptor watching: the stdlib selector loop's transports, sock_*
    # helpers, pipes, and child watchers all reduce to these four calls. The
    # Handle bookkeeping mirrors selector_events so cancellation semantics
    # (a replaced or removed reader never fires) are identical.

    def _add_reader(self, fd, callback, *args):
        self._check_closed()
        fd_key = fd if isinstance(fd, int) else fd.fileno()
        handle = asyncio.Handle(callback, args, self, None)
        previous = self._readers.get(fd_key)
        self._scheduler.add_reader(fd_key, handle)
        self._readers[fd_key] = handle
        if previous is not None:
            previous.cancel()
        return handle

    def _add_writer(self, fd, callback, *args):
        self._check_closed()
        fd_key = fd if isinstance(fd, int) else fd.fileno()
        handle = asyncio.Handle(callback, args, self, None)
        previous = self._writers.get(fd_key)
        self._scheduler.add_writer(fd_key, handle)
        self._writers[fd_key] = handle
        if previous is not None:
            previous.cancel()
        return handle

    def _remove_reader(self, fd):
        if self._closed:
            return False
        fd_key = fd if isinstance(fd, int) else fd.fileno()
        handle = self._readers.pop(fd_key, None)
        if handle is None:
            return False
        handle.cancel()
        self._scheduler.remove_reader(fd_key)
        return True

    def _remove_writer(self, fd):
        if self._closed:
            return False
        fd_key = fd if isinstance(fd, int) else fd.fileno()
        handle = self._writers.pop(fd_key, None)
        if handle is None:
            return False
        handle.cancel()
        self._scheduler.remove_writer(fd_key)
        return True

    def add_signal_handler(self, sig, callback, *args):
        if not callable(callback):
            raise TypeError(f"a callable object was expected, got {callback!r}")
        context = contextvars.copy_context()
        schedule = functools.partial(self.call_soon_threadsafe, callback, *args, context=context)
        self._scheduler.add_signal_handler(sig, schedule)

    def remove_signal_handler(self, sig):
        return self._scheduler.remove_signal_handler(sig)


def make_worker_loop(scheduler):
    return WorkerLoop(scheduler)


def _with_running_loop(loop, callback, *args):
    previous = asyncio.events._get_running_loop()
    asyncio.events._set_running_loop(loop)
    try:
        return callback(*args)
    finally:
        asyncio.events._set_running_loop(previous)


def _worker_dispatch_start(dispatch, request, loop, resolver, task_sink=None):
    def start():
        try:
            coro = dispatch(request)
            # Python 3.12 eager-start Tasks execute inline while establishing
            # real Task identity first. AnyIO/httpx cancellation scopes depend
            # on current_task() being non-None from the first instruction.
            task = loop.create_task(coro, eager_start=loop.get_task_factory() is None)
        except BaseException as exc:
            resolver.set_exception(exc)
            return
        if task_sink is not None:
            task_sink.set_task(task)
        if task.done():
            _resolve_task(task, resolver)
        else:
            task.add_done_callback(lambda done: _resolve_task(done, resolver))

    _with_running_loop(loop, start)


def worker_dispatch_start(dispatch, request, loop, resolver):
    """Start one request and arrange for its resolver to complete."""
    _worker_dispatch_start(dispatch, request, loop, resolver)


def worker_dispatch_start_cancellable(dispatch, request, loop, resolver, task_sink):
    """Like worker_dispatch_start, but hands the created Task to `task_sink`.

    Used by MCP dispatch: rmcp cancels the request's CancellationToken on
    client disconnect, and Rust then schedules `task.cancel()` through the
    task's loop. Plain HTTP routes deliberately keep the non-cancelling path.
    """

    _worker_dispatch_start(dispatch, request, loop, resolver, task_sink)


def worker_sync_dispatch(dispatch, request, loop):
    """Run a trivially-async sync fast path with WorkerLoop APIs available."""
    return _with_running_loop(loop, dispatch, request)


def _resolve_task(task, resolver):
    try:
        resolver.set_result(task.result())
    except BaseException as exc:
        resolver.set_exception(exc)


def mcp_resolve_future(fut, value):
    """Resolve an MCP reply future from Rust, tolerating late resolution.

    Runs on the future's own loop via call_soon_threadsafe. A future belonging
    to a cancelled tool call is already done — resolving it then would raise
    InvalidStateError into the loop's exception handler, so skip it.
    """
    if not fut.done():
        fut.set_result(value)


def worker_handle_failed(loop, handle, exc) -> None:
    """Cold path for the Rust pump, which runs ready handles directly
    (``_cancelled`` check, then ``Handle._run``) with the WorkerLoop installed
    as the running loop for a whole drain batch. ``Handle._run`` reports
    ordinary callback exceptions itself; this catches what it lets escape
    (SystemExit, KeyboardInterrupt, a broken handle)."""
    loop.call_exception_handler({"message": "Exception in WorkerLoop callback", "exception": exc, "handle": handle})
