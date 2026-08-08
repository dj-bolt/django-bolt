"""Process-lived asyncio facade used by Bolt's worker-local HTTP dispatch.

The facade keeps one loop identity for the life of the process.  Python
callbacks are driven by Rust/Tokio, while operations that require a real
selector are submitted to Bolt's existing compatibility asyncio loop.

Callbacks may resume on a different OS thread after a suspension (the Tokio
pump task can migrate between worker threads).  Contextvars are preserved via
Handle contexts; ``threading.local`` state is not carried across awaits.

Synchronous crossings between the two loops (transport writes, ``eof_received``,
reader/writer removal) are bounded by ``DJANGO_BOLT_LOOP_CROSSING_TIMEOUT``
seconds (default 30) so a cross-blocked pair of loops surfaces as a
``RuntimeError`` instead of a silent deadlock.

Known deviation from stdlib loops: ``remove_signal_handler`` stops delivering
the signal to Python but cannot restore the default disposition — Tokio's
process-level signal handler stays installed for the process lifetime, so a
"removed" signal is effectively ignored rather than reset to ``SIG_DFL``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import inspect
import logging
import os
import time

from .concurrency import _get_default_executor

logger = logging.getLogger(__name__)


def _crossing_timeout() -> float:
    raw = os.environ.get("DJANGO_BOLT_LOOP_CROSSING_TIMEOUT")
    try:
        return float(raw) if raw else 30.0
    except ValueError:
        return 30.0


_CROSSING_TIMEOUT = _crossing_timeout()


def _await_crossing(result: concurrent.futures.Future, description: str):
    """Wait on a cross-loop synchronous call, raising instead of deadlocking."""
    try:
        return result.result(_CROSSING_TIMEOUT)
    except TimeoutError:
        raise RuntimeError(
            f"{description} did not complete within {_CROSSING_TIMEOUT}s; "
            "the WorkerLoop/selector-loop crossing may be deadlocked "
            "(DJANGO_BOLT_LOOP_CROSSING_TIMEOUT tunes this limit)"
        ) from None


class _ThreadsafeTransportProxy(asyncio.Transport):
    """Marshal mutating transport operations onto the selector loop."""

    __slots__ = ("_adapter", "_loop", "_protocol", "_transport")

    def __init__(self, loop, transport, protocol, adapter=None):
        self._adapter = adapter
        self._loop = loop
        self._transport = transport
        self._protocol = protocol

    @property
    def _raw_transport(self):
        return self._transport

    def _replace_raw_transport(self, transport):
        self._transport = transport

    def _call(self, name, *args):
        self._loop.call_soon_threadsafe(getattr(self._transport, name), *args)

    def _call_sync(self, name, *args, **kwargs):
        result = concurrent.futures.Future()

        def invoke():
            try:
                result.set_result(getattr(self._transport, name)(*args, **kwargs))
            except BaseException as exc:
                result.set_exception(exc)

        self._loop.call_soon_threadsafe(invoke)
        value = _await_crossing(result, f"selector-loop transport call {name!r}")
        if self._adapter is not None:
            self._adapter.flush_flow_control()
        return value

    def get_extra_info(self, name, default=None):
        return self._transport.get_extra_info(name, default)

    def is_closing(self):
        return self._transport.is_closing()

    def close(self):
        self._call("close")

    def abort(self):
        self._call("abort")

    # Writes cross synchronously so pause_writing state is reflected before
    # StreamWriter.drain() checks it. The round-trip runs on the pump thread,
    # so every write briefly stalls all WorkerLoop callbacks process-wide —
    # the first place to look if chatty stream protocols regress.
    def write(self, data):
        self._call_sync("write", data)

    def writelines(self, list_of_data):
        self._call_sync("writelines", list_of_data)

    def write_eof(self):
        self._call("write_eof")

    def can_write_eof(self):
        return self._transport.can_write_eof()

    def set_write_buffer_limits(self, high=None, low=None):
        self._call_sync("set_write_buffer_limits", high=high, low=low)

    def get_write_buffer_size(self):
        return self._call_sync("get_write_buffer_size")

    def get_write_buffer_limits(self):
        return self._call_sync("get_write_buffer_limits")

    def pause_reading(self):
        self._call("pause_reading")

    def resume_reading(self):
        self._call("resume_reading")

    def is_reading(self):
        return self._transport.is_reading()

    def set_protocol(self, protocol):
        self._protocol = protocol

        def apply():
            raw_protocol = self._transport.get_protocol()
            if isinstance(
                raw_protocol,
                (_SelectorProtocolAdapter, _SelectorBufferedProtocolAdapter),
            ):
                raw_protocol.protocol = protocol
            else:
                self._transport.set_protocol(protocol)

        self._loop.call_soon_threadsafe(apply)

    def get_protocol(self):
        return self._protocol

    def __getattr__(self, name):
        # Preserve access to implementation-specific, read-only transport APIs.
        return getattr(self._transport, name)


class _SelectorProtocolAdapter(asyncio.Protocol):
    """Forward selector-thread protocol callbacks to the WorkerLoop queue."""

    __slots__ = (
        "protocol",
        "raw_write_paused",
        "selector_loop",
        "transport",
        "user_write_paused",
        "worker_loop",
    )

    def __init__(self, worker_loop, selector_loop, protocol):
        self.worker_loop = worker_loop
        self.selector_loop = selector_loop
        self.protocol = protocol
        self.raw_write_paused = False
        self.transport = None
        self.user_write_paused = False

    def _forward(self, name, *args):
        callback = getattr(self.protocol, name, None)
        if callback is not None:
            self.worker_loop.call_soon_threadsafe(callback, *args)

    def connection_made(self, transport):
        self.transport = _ThreadsafeTransportProxy(self.selector_loop, transport, self.protocol, self)
        self._forward("connection_made", self.transport)

    def connection_lost(self, exc):
        self._forward("connection_lost", exc)

    def data_received(self, data):
        self._forward("data_received", data)

    def eof_received(self):
        callback = getattr(self.protocol, "eof_received", None)
        if callback is None:
            return False
        return self.worker_loop._call_worker_sync(callback)

    def pause_writing(self):
        self.raw_write_paused = True
        self.worker_loop.call_soon_threadsafe(self.flush_flow_control)

    def resume_writing(self):
        self.raw_write_paused = False
        self.worker_loop.call_soon_threadsafe(self.flush_flow_control)

    def flush_flow_control(self):
        if self.raw_write_paused == self.user_write_paused:
            return
        self.user_write_paused = self.raw_write_paused
        callback_name = "pause_writing" if self.raw_write_paused else "resume_writing"
        callback = getattr(self.protocol, callback_name, None)
        if callback is not None:
            callback()


class _SelectorBufferedProtocolAdapter(asyncio.BufferedProtocol):
    __slots__ = (
        "protocol",
        "raw_write_paused",
        "selector_loop",
        "transport",
        "user_write_paused",
        "worker_loop",
    )

    def __init__(self, worker_loop, selector_loop, protocol):
        self.worker_loop = worker_loop
        self.selector_loop = selector_loop
        self.protocol = protocol
        self.raw_write_paused = False
        self.transport = None
        self.user_write_paused = False

    def _forward(self, name, *args):
        callback = getattr(self.protocol, name, None)
        if callback is not None:
            self.worker_loop.call_soon_threadsafe(callback, *args)

    def connection_made(self, transport):
        self.transport = _ThreadsafeTransportProxy(self.selector_loop, transport, self.protocol, self)
        self._forward("connection_made", self.transport)

    def connection_lost(self, exc):
        self._forward("connection_lost", exc)

    def get_buffer(self, sizehint):
        # Selectors require the writable buffer synchronously. Implementations
        # only expose storage here; stateful processing remains in
        # buffer_updated(), which is forwarded to WorkerLoop.
        return self.protocol.get_buffer(sizehint)

    def buffer_updated(self, nbytes):
        self._forward("buffer_updated", nbytes)

    def eof_received(self):
        callback = getattr(self.protocol, "eof_received", None)
        if callback is None:
            return False
        return self.worker_loop._call_worker_sync(callback)

    def pause_writing(self):
        self.raw_write_paused = True
        self.worker_loop.call_soon_threadsafe(self.flush_flow_control)

    def resume_writing(self):
        self.raw_write_paused = False
        self.worker_loop.call_soon_threadsafe(self.flush_flow_control)

    def flush_flow_control(self):
        if self.raw_write_paused == self.user_write_paused:
            return
        self.user_write_paused = self.raw_write_paused
        callback_name = "pause_writing" if self.raw_write_paused else "resume_writing"
        callback = getattr(self.protocol, callback_name, None)
        if callback is not None:
            callback()


def _make_stream_adapter(worker_loop, selector_loop, protocol):
    adapter_type = (
        _SelectorBufferedProtocolAdapter if isinstance(protocol, asyncio.BufferedProtocol) else _SelectorProtocolAdapter
    )
    return adapter_type(worker_loop, selector_loop, protocol)


class _ThreadsafeDatagramTransportProxy(_ThreadsafeTransportProxy, asyncio.DatagramTransport):
    def sendto(self, data, addr=None):
        if addr is None:
            self._call("sendto", data)
        else:
            self._call("sendto", data, addr)


class _SelectorDatagramProtocolAdapter(asyncio.DatagramProtocol):
    __slots__ = ("worker_loop", "selector_loop", "protocol", "transport")

    def __init__(self, worker_loop, selector_loop, protocol):
        self.worker_loop = worker_loop
        self.selector_loop = selector_loop
        self.protocol = protocol
        self.transport = None

    def _forward(self, name, *args):
        callback = getattr(self.protocol, name, None)
        if callback is not None:
            self.worker_loop.call_soon_threadsafe(callback, *args)

    def connection_made(self, transport):
        self.transport = _ThreadsafeDatagramTransportProxy(self.selector_loop, transport, self.protocol)
        self._forward("connection_made", self.transport)

    def connection_lost(self, exc):
        self._forward("connection_lost", exc)

    def datagram_received(self, data, addr):
        self._forward("datagram_received", data, addr)

    def error_received(self, exc):
        self._forward("error_received", exc)

    # Unlike the stream adapters there is no synchronous write crossing here
    # (sendto is fire-and-forget, and datagrams have no drain() race), so
    # plain queue forwarding preserves pause/resume ordering.
    def pause_writing(self):
        self._forward("pause_writing")

    def resume_writing(self):
        self._forward("resume_writing")


class _ThreadsafeSubprocessTransportProxy(asyncio.SubprocessTransport):
    """Subprocess transport facade whose exit wait crosses to the selector loop.

    ``BaseSubprocessTransport._wait()`` creates its exit waiter on the loop
    that owns the transport (the selector loop).  Awaiting that future from a
    WorkerLoop task raises "attached to a different loop" whenever ``wait()``
    overtakes child reaping, so the wait runs on the selector loop and its
    completion is bridged back like every other selector-backed operation.
    """

    __slots__ = ("_transport", "_worker_loop")

    def __init__(self, worker_loop, transport):
        self._worker_loop = worker_loop
        self._transport = transport

    async def _wait(self):
        returncode = self._transport.get_returncode()
        if returncode is not None:
            return returncode
        return await self._worker_loop._submit_selector(self._wait_on_selector_loop())

    async def _wait_on_selector_loop(self):
        # Runs on the selector loop: stdlib _wait() returns a coroutine while
        # uvloop's returns a Future, and both must be awaited on the loop that
        # owns the transport.
        return await self._transport._wait()

    def get_extra_info(self, name, default=None):
        return self._transport.get_extra_info(name, default)

    def is_closing(self):
        return self._transport.is_closing()

    def close(self):
        # close() cascades into pipe-transport close() calls that schedule on
        # the owning loop, so it must run on the selector thread.
        self._worker_loop._selector_loop.call_soon_threadsafe(self._transport.close)

    def get_pid(self):
        return self._transport.get_pid()

    def get_returncode(self):
        return self._transport.get_returncode()

    def get_pipe_transport(self, fd):
        return self._transport.get_pipe_transport(fd)

    def send_signal(self, signal):
        self._transport.send_signal(signal)

    def terminate(self):
        self._transport.terminate()

    def kill(self):
        self._transport.kill()

    def __getattr__(self, name):
        return getattr(self._transport, name)


class _ThreadsafeServerProxy:
    __slots__ = ("_loop", "_server")

    def __init__(self, loop, server):
        self._loop = loop
        self._server = server

    @property
    def sockets(self):
        return self._server.sockets

    def get_loop(self):
        # Futures and waiters interacting with this server must live on the
        # WorkerLoop, not on the selector loop that owns the raw server.
        return self._loop

    def is_serving(self):
        return self._server.is_serving()

    def close(self):
        self._loop._selector_loop.call_soon_threadsafe(self._server.close)

    def close_clients(self):
        close_clients = self._server.close_clients  # AttributeError before 3.13
        self._loop._selector_loop.call_soon_threadsafe(close_clients)

    def abort_clients(self):
        abort_clients = self._server.abort_clients  # AttributeError before 3.13
        self._loop._selector_loop.call_soon_threadsafe(abort_clients)

    async def start_serving(self):
        await self._loop._submit_selector(self._server.start_serving())

    async def serve_forever(self):
        await self._loop._submit_selector(self._server.serve_forever())

    async def wait_closed(self):
        await self._loop._submit_selector(self._server.wait_closed())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.close()
        await self.wait_closed()


class WorkerLoop(asyncio.AbstractEventLoop):
    """An asyncio loop whose ready queue is serviced by a Tokio task.

    There is one instance per process, so tasks may outlive an HTTP response
    and loop-bound objects can safely be reused by later requests.  Selector,
    socket, DNS, and subprocess work runs on ``selector_loop``; completions
    are forwarded back to this loop's ready queue.
    """

    __slots__ = (
        "_scheduler",
        "_selector_loop",
        "_closed",
        "_debug",
        "_default_executor",
        "_exception_handler",
        "_readers",
        "_task_factory",
        "_writers",
    )

    def __init__(self, scheduler, selector_loop):
        self._scheduler = scheduler
        self._selector_loop = selector_loop
        self._closed = False
        self._debug = False
        self._default_executor = _get_default_executor()
        self._exception_handler = None
        self._readers = set()
        self._task_factory = None
        self._writers = set()

    def time(self):
        return time.monotonic()

    def is_running(self):
        return not self._closed

    def is_closed(self):
        return self._closed

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

    def _check_closed(self):
        if self._closed:
            raise RuntimeError("Event loop is closed")

    def get_debug(self):
        return self._debug

    def set_debug(self, enabled):
        self._debug = bool(enabled)

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

    def create_future(self):
        return asyncio.Future(loop=self)

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

    def set_task_factory(self, factory):
        self._task_factory = factory

    def get_task_factory(self):
        return self._task_factory

    def run_in_executor(self, executor, func, *args):
        self._check_closed()
        if executor is None:
            executor = self._default_executor
        return asyncio.wrap_future(executor.submit(func, *args), loop=self)

    def set_default_executor(self, executor):
        if not isinstance(executor, concurrent.futures.ThreadPoolExecutor):
            raise TypeError("executor must be ThreadPoolExecutor instance")
        self._default_executor = executor

    def _submit_selector(self, coroutine):
        """Submit selector-backed work and return a Future bound to this loop."""
        self._check_closed()
        future = asyncio.run_coroutine_threadsafe(coroutine, self._selector_loop)
        return asyncio.wrap_future(future, loop=self)

    def _call_worker_sync(self, callback, *args, context=None):
        result = concurrent.futures.Future()

        def invoke():
            try:
                result.set_result(callback(*args))
            except BaseException as exc:
                result.set_exception(exc)

        self.call_soon_threadsafe(invoke, context=context)
        return _await_crossing(result, f"WorkerLoop call {callback!r}")

    def _adapt_protocol_factory(self, protocol_factory, *, datagram=False):
        context = contextvars.copy_context()
        adapter_type = _SelectorDatagramProtocolAdapter if datagram else None

        def factory():
            protocol = self._call_worker_sync(protocol_factory, context=context)
            if adapter_type is not None:
                return adapter_type(self, self._selector_loop, protocol)
            return _make_stream_adapter(self, self._selector_loop, protocol)

        return factory

    async def create_connection(self, protocol_factory, host=None, port=None, **kwargs):
        protocol = protocol_factory()
        adapter = _make_stream_adapter(self, self._selector_loop, protocol)
        await self._submit_selector(self._selector_loop.create_connection(lambda: adapter, host, port, **kwargs))
        return adapter.transport, protocol

    async def create_unix_connection(self, protocol_factory, path=None, **kwargs):
        protocol = protocol_factory()
        adapter = _make_stream_adapter(self, self._selector_loop, protocol)
        await self._submit_selector(self._selector_loop.create_unix_connection(lambda: adapter, path, **kwargs))
        return adapter.transport, protocol

    async def create_server(self, protocol_factory, host=None, port=None, **kwargs):
        server = await self._submit_selector(
            self._selector_loop.create_server(self._adapt_protocol_factory(protocol_factory), host, port, **kwargs)
        )
        return _ThreadsafeServerProxy(self, server)

    async def create_unix_server(self, protocol_factory, path=None, **kwargs):
        server = await self._submit_selector(
            self._selector_loop.create_unix_server(self._adapt_protocol_factory(protocol_factory), path, **kwargs)
        )
        return _ThreadsafeServerProxy(self, server)

    async def connect_accepted_socket(self, protocol_factory, sock, **kwargs):
        protocol = protocol_factory()
        adapter = _make_stream_adapter(self, self._selector_loop, protocol)
        await self._submit_selector(self._selector_loop.connect_accepted_socket(lambda: adapter, sock, **kwargs))
        return adapter.transport, protocol

    async def create_datagram_endpoint(self, protocol_factory, *args, **kwargs):
        protocol = protocol_factory()
        adapter = _SelectorDatagramProtocolAdapter(self, self._selector_loop, protocol)
        await self._submit_selector(self._selector_loop.create_datagram_endpoint(lambda: adapter, *args, **kwargs))
        return adapter.transport, protocol

    async def sendfile(self, transport, file, offset=0, count=None, *, fallback=True):
        raw_transport = getattr(transport, "_raw_transport", transport)
        return await self._submit_selector(
            self._selector_loop.sendfile(raw_transport, file, offset=offset, count=count, fallback=fallback)
        )

    async def start_tls(self, transport, protocol, sslcontext, **kwargs):
        if not isinstance(transport, _ThreadsafeTransportProxy):
            raise TypeError("start_tls() requires a WorkerLoop transport")
        raw_transport = transport._raw_transport
        adapter = raw_transport.get_protocol()
        if not isinstance(adapter, (_SelectorProtocolAdapter, _SelectorBufferedProtocolAdapter)):
            raise TypeError("transport protocol is not managed by WorkerLoop")
        raw_tls_transport = await self._submit_selector(
            self._selector_loop.start_tls(raw_transport, adapter, sslcontext, **kwargs)
        )
        transport._replace_raw_transport(raw_tls_transport)
        adapter.transport = transport
        return transport

    async def connect_read_pipe(self, protocol_factory, pipe):
        protocol = protocol_factory()
        adapter = _make_stream_adapter(self, self._selector_loop, protocol)
        await self._submit_selector(self._selector_loop.connect_read_pipe(lambda: adapter, pipe))
        return adapter.transport, protocol

    async def connect_write_pipe(self, protocol_factory, pipe):
        protocol = protocol_factory()
        adapter = _make_stream_adapter(self, self._selector_loop, protocol)
        await self._submit_selector(self._selector_loop.connect_write_pipe(lambda: adapter, pipe))
        return adapter.transport, protocol

    async def sock_recv(self, sock, nbytes):
        return await self._submit_selector(self._selector_loop.sock_recv(sock, nbytes))

    async def sock_recv_into(self, sock, buf):
        return await self._submit_selector(self._selector_loop.sock_recv_into(sock, buf))

    async def sock_recvfrom(self, sock, bufsize):
        return await self._submit_selector(self._selector_loop.sock_recvfrom(sock, bufsize))

    async def sock_recvfrom_into(self, sock, buf, nbytes=0):
        return await self._submit_selector(self._selector_loop.sock_recvfrom_into(sock, buf, nbytes))

    async def sock_sendall(self, sock, data):
        return await self._submit_selector(self._selector_loop.sock_sendall(sock, data))

    async def sock_sendto(self, sock, data, address):
        return await self._submit_selector(self._selector_loop.sock_sendto(sock, data, address))

    async def sock_connect(self, sock, address):
        return await self._submit_selector(self._selector_loop.sock_connect(sock, address))

    async def sock_accept(self, sock):
        return await self._submit_selector(self._selector_loop.sock_accept(sock))

    async def sock_sendfile(self, sock, file, offset=0, count=None, *, fallback=None):
        return await self._submit_selector(
            self._selector_loop.sock_sendfile(sock, file, offset=offset, count=count, fallback=fallback)
        )

    async def getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        return await self._submit_selector(
            self._selector_loop.getaddrinfo(host, port, family=family, type=type, proto=proto, flags=flags)
        )

    async def getnameinfo(self, sockaddr, flags=0):
        return await self._submit_selector(self._selector_loop.getnameinfo(sockaddr, flags))

    async def subprocess_exec(self, protocol_factory, *args, **kwargs):
        transport, protocol = await self._submit_selector(
            self._selector_loop.subprocess_exec(protocol_factory, *args, **kwargs)
        )
        return _ThreadsafeSubprocessTransportProxy(self, transport), protocol

    async def subprocess_shell(self, protocol_factory, cmd, **kwargs):
        transport, protocol = await self._submit_selector(
            self._selector_loop.subprocess_shell(protocol_factory, cmd, **kwargs)
        )
        return _ThreadsafeSubprocessTransportProxy(self, transport), protocol

    def _selector_callback(self, callback, args):
        self.call_soon_threadsafe(callback, *args)

    def add_reader(self, fd, callback, *args):
        fd_key = fd if isinstance(fd, int) else fd.fileno()
        self._readers.add(fd_key)
        self._selector_loop.call_soon_threadsafe(
            self._selector_loop.add_reader,
            fd,
            self._selector_callback,
            callback,
            args,
        )

    def add_writer(self, fd, callback, *args):
        fd_key = fd if isinstance(fd, int) else fd.fileno()
        self._writers.add(fd_key)
        self._selector_loop.call_soon_threadsafe(
            self._selector_loop.add_writer,
            fd,
            self._selector_callback,
            callback,
            args,
        )

    def _call_selector_sync(self, callback, *args):
        result = concurrent.futures.Future()

        def invoke():
            try:
                result.set_result(callback(*args))
            except BaseException as exc:
                result.set_exception(exc)

        self._selector_loop.call_soon_threadsafe(invoke)
        return _await_crossing(result, f"selector-loop call {callback!r}")

    def remove_reader(self, fd):
        fd_key = fd if isinstance(fd, int) else fd.fileno()
        existed = fd_key in self._readers
        self._readers.discard(fd_key)
        self._call_selector_sync(self._selector_loop.remove_reader, fd)
        return existed

    def remove_writer(self, fd):
        fd_key = fd if isinstance(fd, int) else fd.fileno()
        existed = fd_key in self._writers
        self._writers.discard(fd_key)
        self._call_selector_sync(self._selector_loop.remove_writer, fd)
        return existed

    def add_signal_handler(self, sig, callback, *args):
        if not callable(callback):
            raise TypeError(f"a callable object was expected, got {callback!r}")
        context = contextvars.copy_context()
        schedule = functools.partial(self.call_soon_threadsafe, callback, *args, context=context)
        self._scheduler.add_signal_handler(sig, schedule)

    def remove_signal_handler(self, sig):
        return self._scheduler.remove_signal_handler(sig)

    def set_exception_handler(self, handler):
        self._exception_handler = handler

    def get_exception_handler(self):
        return self._exception_handler

    def default_exception_handler(self, context):
        exception = context.get("exception")
        logger.error(
            "worker asyncio callback failed: %s%s",
            context.get("message"),
            f": {exception}" if exception is not None else "",
            exc_info=exception,
        )

    def call_exception_handler(self, context):
        if self._exception_handler is None:
            self.default_exception_handler(context)
        else:
            self._exception_handler(self, context)


def make_worker_loop(scheduler, selector_loop):
    return WorkerLoop(scheduler, selector_loop)


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


def run_worker_handle(loop, handle):
    """Execute one ready Handle with the WorkerLoop installed as running."""
    if handle.cancelled():
        return
    try:
        _with_running_loop(loop, handle._run)
    except BaseException as exc:
        loop.call_exception_handler({"message": "Exception in WorkerLoop callback", "exception": exc, "handle": handle})
