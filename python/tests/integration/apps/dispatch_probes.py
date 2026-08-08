"""Dispatch-path probe app: one route per Rust↔Python dispatch mechanism.

Used two ways:
- Correctness: the eager loop-thread dispatch integration tests drive every
  async-bridge shape through a real ``runbolt`` server (TestClient uses a
  different bridge, so only a subprocess exercises the production path).
- Measurement: point a ``runbolt`` server at this module and time these routes
  over a keepalive connection to price each dispatch stage (see
  docs/PROFILING.md). ``t_ready - t_trivial`` is the pure async-bridge cost;
  ``t_sleep0 - t_ready`` is one real suspend.

Probe map:
- /t-sync     sync def                    → sync-dispatch bypass
- /t-trivial  async def, no await         → trivially-async sync dispatch
- /t-ready    awaits, never suspends      → eager dispatch completes inline
- /t-sleep0   await asyncio.sleep(0)      → eager start + bare-yield reschedule
- /t-timer    await asyncio.sleep(1ms)    → timer scheduling
- /t-cancelled-timers + /t-timer-stats    → cancelled-timer heap compaction
- /t-crossing-timeout                     → bounded cross-loop sync crossings
- /t-thread   await sync_to_thread(...)   → eager start + real Future suspension
- /t-subprocess-wait  wait() on live child → pending exit waiter crosses loops
- /t-exc      HTTPException after await   → exception through the driver Task
- /t-deps     two async Depends           → asyncio.gather in the first segment
- /t-task     create_task in first segment → loop APIs during eager execution
- /t-stream   async generator (NDJSON)    → streaming wire through async path
- /t-server-api                           → Server.get_loop()/close_clients() proxying
- /t-datagram-flow-control                → pause/resume_writing reaches datagram protocols
- /t-call-soon-coroutine                  → debug-mode call_soon(coroutine) rejection
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import ssl
import sys
import time
from typing import Annotated

import httpx

from django_bolt import BoltAPI, _core
from django_bolt.concurrency import sync_to_thread
from django_bolt.exceptions import HTTPException
from django_bolt.params import Depends

api = BoltAPI()
PAYLOAD = {"message": "hello", "n": 1}
_background_done = False
_shared_lock = asyncio.Lock()
_http_client = None


async def _noop():
    return None


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/t-sync")
def t_sync():
    return PAYLOAD


@api.get("/t-trivial")
async def t_trivial():
    return PAYLOAD


@api.get("/t-ready")
async def t_ready():
    await _noop()
    return PAYLOAD


@api.get("/t-sleep0")
async def t_sleep0():
    await asyncio.sleep(0)
    return PAYLOAD


@api.get("/t-timer")
async def t_timer():
    await asyncio.sleep(0.001)
    return PAYLOAD


@api.get("/t-cancelled-timers")
async def t_cancelled_timers():
    loop = asyncio.get_running_loop()
    handles = [loop.call_later(3600, lambda: None) for _ in range(200)]
    for handle in handles:
        handle.cancel()
    return {"scheduled": len(handles)}


@api.get("/t-timer-stats")
async def t_timer_stats():
    return {"live": _core.worker_timer_count()}


@api.get("/t-oversized-timer")
async def t_oversized_timer():
    """A finite but Duration-overflowing delay must raise OverflowError from
    the WorkerLoop scheduler, not panic (which aborts release wheels)."""
    loop = asyncio.get_running_loop()
    try:
        handle = loop.call_later(1e300, lambda: None)
    except OverflowError:
        return {"raised": "OverflowError"}
    handle.cancel()
    return {"raised": None}


@api.get("/t-crossing-timeout")
async def t_crossing_timeout():
    loop = asyncio.get_running_loop()
    reader, writer = await asyncio.open_connection("127.0.0.1", int(os.environ["BOLT_PROBE_TCP_PORT"]))
    try:
        await reader.readline()
        # Wedge the selector loop past the crossing timeout, then attempt a
        # synchronous crossing (transport write). It must raise, not hang.
        loop._selector_loop.call_soon_threadsafe(time.sleep, 4)
        try:
            writer.write(b"ping\n")
        except RuntimeError as exc:
            return {"timed_out": "did not complete within" in str(exc)}
        return {"timed_out": False}
    finally:
        writer.close()


@api.get("/t-thread")
async def t_thread():
    value = await sync_to_thread(lambda: "from-thread")
    return {"value": value}


@api.get("/t-subprocess")
async def t_subprocess():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "print('from-subprocess')",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    return {"value": stdout.decode().strip(), "returncode": process.returncode}


@api.get("/t-subprocess-wait")
async def t_subprocess_wait():
    # wait() runs while the child is still alive, forcing the pending
    # exit-waiter path in the subprocess transport. /t-subprocess only hits
    # that path when wait() overtakes child reaping (a load-dependent race);
    # this probe covers it deterministically.
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(0.3)")
    returncode = await process.wait()
    return {"returncode": returncode}


@api.get("/t-exc")
async def t_exc():
    await asyncio.sleep(0)
    raise HTTPException(status_code=418, detail="teapot-after-await")


async def _dep_a() -> str:
    await asyncio.sleep(0)
    return "a"


async def _dep_b() -> str:
    await asyncio.sleep(0)
    return "b"


@api.get("/t-deps")
async def t_deps(a: Annotated[str, Depends(_dep_a)], b: Annotated[str, Depends(_dep_b)]):
    return {"a": a, "b": b}


@api.get("/t-task")
async def t_task():
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(_dep_a())
    return {"loop_running": loop.is_running(), "task_result": await task}


@api.get("/t-socket")
async def t_socket():
    reader, writer = await asyncio.open_connection("127.0.0.1", int(os.environ["BOLT_PROBE_TCP_PORT"]))
    try:
        greeting = await reader.readline()
        writer.write(b"ping\n")
        await writer.drain()
        return {"greeting": greeting.decode().strip()}
    finally:
        writer.close()
        await writer.wait_closed()


@api.get("/t-buffered-protocol")
async def t_buffered_protocol():
    loop = asyncio.get_running_loop()
    received = loop.create_future()

    class Protocol(asyncio.BufferedProtocol):
        def __init__(self):
            self.buffer = bytearray(64)

        def connection_made(self, transport):
            self.transport = transport

        def get_buffer(self, sizehint):
            return self.buffer

        def buffer_updated(self, nbytes):
            if not received.done():
                received.set_result(bytes(self.buffer[:nbytes]))
                self.transport.write(b"ping\n")

    transport, _ = await loop.create_connection(Protocol, "127.0.0.1", int(os.environ["BOLT_PROBE_TCP_PORT"]))
    try:
        return {"greeting": (await asyncio.wait_for(received, 1)).decode().strip()}
    finally:
        transport.close()


@api.get("/t-start-tls")
async def t_start_tls():
    reader, writer = await asyncio.open_connection("127.0.0.1", int(os.environ["BOLT_PROBE_STARTTLS_PORT"]))
    try:
        writer.write(b"STARTTLS\n")
        await writer.drain()
        assert await reader.readline() == b"READY\n"
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        await writer.start_tls(context)
        writer.write(b"over-tls\n")
        await writer.drain()
        return {"reply": (await reader.readline()).decode().strip()}
    finally:
        writer.close()
        await writer.wait_closed()


@api.get("/t-backpressure")
async def t_backpressure():
    reader, writer = await asyncio.open_connection("127.0.0.1", int(os.environ["BOLT_PROBE_SLOW_SINK_PORT"]))
    payload = b"x" * (8 * 1024 * 1024)
    try:
        sock = writer.transport.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        writer.transport.set_write_buffer_limits(high=1024, low=512)
        started = time.monotonic()
        writer.write(len(payload).to_bytes(8, "big") + payload)
        await writer.drain()
        drain_seconds = time.monotonic() - started
        received = int.from_bytes(await reader.readexactly(8), "big")
        return {"received": received, "backpressure_observed": drain_seconds >= 0.05}
    finally:
        writer.close()
        await writer.wait_closed()


@api.get("/t-pipes")
async def t_pipes():
    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    read_pipe = os.fdopen(read_fd, "rb", buffering=0)
    write_pipe = os.fdopen(write_fd, "wb", buffering=0)
    received = loop.create_future()

    class ReadProtocol(asyncio.Protocol):
        def data_received(self, data):
            if not received.done():
                received.set_result(data)

    read_transport = write_transport = None
    try:
        read_transport, _ = await loop.connect_read_pipe(ReadProtocol, read_pipe)
        write_transport, _ = await loop.connect_write_pipe(asyncio.Protocol, write_pipe)
        write_transport.write(b"through-pipes")
        data = await asyncio.wait_for(received, 1)
        return {"value": data.decode()}
    finally:
        if write_transport is not None:
            write_transport.close()
        if read_transport is not None:
            read_transport.close()


@api.get("/t-fd-watcher")
async def t_fd_watcher():
    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    received = loop.create_future()
    writable = loop.create_future()
    write_sock, peer_sock = socket.socketpair()

    def readable():
        if not received.done():
            received.set_result(os.read(read_fd, 1024))

    try:
        loop.add_reader(read_fd, readable)
        loop.add_writer(
            write_sock,
            lambda: writable.set_result(True) if not writable.done() else None,
        )
        os.write(write_fd, b"fd-ready")
        data = await asyncio.wait_for(received, 1)
        await asyncio.wait_for(writable, 1)
        return {
            "value": data.decode(),
            "reader_removed": loop.remove_reader(read_fd),
            "writer_removed": loop.remove_writer(write_sock),
        }
    finally:
        loop.remove_reader(read_fd)
        loop.remove_writer(write_sock)
        os.close(read_fd)
        os.close(write_fd)
        write_sock.close()
        peer_sock.close()


@api.get("/t-signal")
async def t_signal():
    loop = asyncio.get_running_loop()
    received = asyncio.Event()
    loop.add_signal_handler(signal.SIGUSR1, received.set)
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        await asyncio.wait_for(received.wait(), 1)
    finally:
        removed = loop.remove_signal_handler(signal.SIGUSR1)
    return {"received": received.is_set(), "removed": removed}


@api.get("/t-worker-server")
async def t_worker_server():
    async def echo(reader, writer):
        writer.write(await reader.readline())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"worker-server\n")
        await writer.drain()
        reply = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return {"reply": reply.decode().strip()}
    finally:
        server.close()
        await server.wait_closed()


@api.get("/t-datagram")
async def t_datagram():
    loop = asyncio.get_running_loop()
    reply = loop.create_future()

    class EchoProtocol(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            self.transport.sendto(data, addr)

    class ClientProtocol(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not reply.done():
                reply.set_result(data)

    server_transport, _ = await loop.create_datagram_endpoint(EchoProtocol, local_addr=("127.0.0.1", 0))
    client_transport = None
    try:
        port = server_transport.get_extra_info("sockname")[1]
        client_transport, _ = await loop.create_datagram_endpoint(ClientProtocol, remote_addr=("127.0.0.1", port))
        client_transport.sendto(b"datagram-ok")
        return {"reply": (await asyncio.wait_for(reply, 1)).decode()}
    finally:
        if client_transport is not None:
            client_transport.close()
        server_transport.close()


@api.get("/t-server-api")
async def t_server_api():
    loop = asyncio.get_running_loop()

    async def hold_open(reader, writer):
        writer.write(b"\0")
        await writer.drain()
        try:
            await reader.read()
        finally:
            writer.close()

    server = await asyncio.start_server(hold_open, "127.0.0.1", 0)
    try:
        get_loop_ok = server.get_loop() is loop
        close_clients_supported = True
        close_clients_eof = None
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            await asyncio.wait_for(reader.readexactly(1), 1)
            try:
                close_clients = server.close_clients
            except AttributeError:
                # Older event loops may not implement Python 3.13's Server API.
                close_clients_supported = False
            else:
                close_clients()
                close_clients_eof = await asyncio.wait_for(reader.read(), 1) == b""
        finally:
            writer.close()
            await writer.wait_closed()
        return {
            "get_loop_is_running_loop": get_loop_ok,
            "close_clients_supported": close_clients_supported,
            "close_clients_eof": close_clients_eof,
        }
    finally:
        server.close()
        await server.wait_closed()


@api.get("/t-datagram-flow-control")
async def t_datagram_flow_control():
    loop = asyncio.get_running_loop()
    paused = loop.create_future()
    resumed = loop.create_future()

    class Protocol(asyncio.DatagramProtocol):
        def pause_writing(self):
            if not paused.done():
                paused.set_result(True)

        def resume_writing(self):
            if not resumed.done():
                resumed.set_result(True)

    transport, _ = await loop.create_datagram_endpoint(Protocol, local_addr=("127.0.0.1", 0))
    try:
        # Kernel UDP send buffers cannot be filled deterministically, so
        # trigger the flow-control callbacks the way the selector transport
        # does: from the selector thread, against the protocol object the raw
        # transport owns (the WorkerLoop adapter, which must forward them).
        raw_transport = getattr(transport, "_raw_transport", transport)
        selector_protocol = raw_transport.get_protocol()
        selector_loop = getattr(loop, "_selector_loop", loop)
        selector_loop.call_soon_threadsafe(selector_protocol.pause_writing)
        await asyncio.wait_for(paused, 1)
        selector_loop.call_soon_threadsafe(selector_protocol.resume_writing)
        await asyncio.wait_for(resumed, 1)
        return {"paused": True, "resumed": True}
    finally:
        transport.close()


@api.get("/t-call-soon-coroutine")
async def t_call_soon_coroutine():
    loop = asyncio.get_running_loop()
    coro = _noop()
    loop.set_debug(True)
    try:
        loop.call_soon(coro)
    except TypeError:
        return {"raised": "TypeError"}
    finally:
        loop.set_debug(False)
        coro.close()
    return {"raised": None}


@api.get("/t-background-start")
async def t_background_start():
    global _background_done
    _background_done = False

    async def finish_later():
        global _background_done
        await asyncio.sleep(0.03)
        _background_done = True

    asyncio.create_task(finish_later())
    return {"started": True}


@api.get("/t-background-status")
async def t_background_status():
    return {"done": _background_done}


@api.get("/t-shared-lock")
async def t_shared_lock():
    async with _shared_lock:
        await asyncio.sleep(0.02)
        return {"loop": id(asyncio.get_running_loop())}


@api.get("/t-cached-client")
async def t_cached_client():
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(verify=False)
    response = await _http_client.get(os.environ["BOLT_PROBE_HTTP_URL"])
    return response.json()


@api.get("/t-stream")
async def t_stream():
    async def gen():
        for i in range(3):
            await asyncio.sleep(0)
            yield f'{{"i": {i}}}\n'.encode()

    return gen()
