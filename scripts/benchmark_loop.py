"""WorkerLoop vs uvloop vs stdlib: per-operation cost of the loop machinery.

Runs each workload inside a real ``runbolt`` worker (the WorkerLoop only exists
there — ``TestClient`` dispatches async handlers on a stdlib background loop),
then the same coroutine under uvloop (if installed) and the stdlib selector
loop in this process. Prints µs/op (best of REPS) and the worker/uvloop ratio.

    just bench-loop            # or: uv run --with uvloop python scripts/benchmark_loop.py
    N=50000 REPS=7 uv run python scripts/benchmark_loop.py reader_events

Workloads:
- call_soon_chain   ready-queue cost, no I/O
- sleep0_chain      one suspend/resume per iteration
- reader_events     one add_reader readiness event per iteration (pipe)
- sock_pingpong     sock_sendall/sock_recv on a socketpair (add/remove_reader churn)
- stream_pingpong   StreamReader/Writer echo over loopback TCP (asyncio's Python
                    transports; uvloop's Cython transports are its main win here)
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
from pathlib import Path

N = int(os.environ.get("N", "20000"))
REPS = int(os.environ.get("REPS", "5"))
PORT = int(os.environ.get("PORT", "18777"))


async def call_soon_chain(n: int) -> float:
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    i = 0

    def cb():
        nonlocal i
        i += 1
        if i < n:
            loop.call_soon(cb)
        else:
            done.set_result(None)

    t0 = time.perf_counter()
    loop.call_soon(cb)
    await done
    return time.perf_counter() - t0


async def sleep0_chain(n: int) -> float:
    t0 = time.perf_counter()
    for _ in range(n):
        await asyncio.sleep(0)
    return time.perf_counter() - t0


async def reader_events(n: int) -> float:
    loop = asyncio.get_running_loop()
    rfd, wfd = os.pipe()
    os.set_blocking(rfd, False)
    done = loop.create_future()
    count = 0

    def cb():
        nonlocal count
        os.read(rfd, 1)
        count += 1
        if count < n:
            os.write(wfd, b"x")
        elif not done.done():
            done.set_result(None)

    loop.add_reader(rfd, cb)
    t0 = time.perf_counter()
    os.write(wfd, b"x")
    await done
    dt = time.perf_counter() - t0
    loop.remove_reader(rfd)
    os.close(rfd)
    os.close(wfd)
    return dt


async def sock_pingpong(n: int) -> float:
    loop = asyncio.get_running_loop()
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)

    async def echo():
        for _ in range(n):
            await loop.sock_sendall(b, await loop.sock_recv(b, 16))

    task = asyncio.ensure_future(echo())
    t0 = time.perf_counter()
    for _ in range(n):
        await loop.sock_sendall(a, b"x")
        await loop.sock_recv(a, 16)
    dt = time.perf_counter() - t0
    await task
    a.close()
    b.close()
    return dt


async def stream_pingpong(n: int) -> float:
    async def handle(reader, writer):
        try:
            while True:
                writer.write(await reader.readexactly(1))
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    t0 = time.perf_counter()
    for _ in range(n):
        writer.write(b"x")
        await reader.readexactly(1)
    dt = time.perf_counter() - t0
    writer.close()
    await writer.wait_closed()
    server.close()
    await server.wait_closed()
    return dt


WORKLOADS = {f.__name__: f for f in (call_soon_chain, sleep0_chain, reader_events, sock_pingpong, stream_pingpong)}


def _write_project(root: Path) -> None:
    (root / "proj").mkdir()
    (root / "proj" / "__init__.py").write_text("")
    (root / "proj" / "urls.py").write_text("urlpatterns = []\n")
    (root / "proj" / "settings.py").write_text(
        textwrap.dedent(
            """
            SECRET_KEY = "bench"
            DEBUG = False
            ALLOWED_HOSTS = ["*"]
            INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth", "django_bolt"]
            DATABASES = {}
            ROOT_URLCONF = "proj.urls"
            BOLT_API = ["benchapi:api"]
            """
        )
    )
    (root / "benchapi.py").write_text(
        textwrap.dedent(
            f"""
            import asyncio
            import importlib.util
            from django_bolt import BoltAPI

            spec = importlib.util.spec_from_file_location("benchmark_loop", {str(Path(__file__).resolve())!r})
            workloads = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(workloads)

            api = BoltAPI()

            @api.get("/health")
            async def health():
                return {{"loop": type(asyncio.get_running_loop()).__name__}}

            @api.get("/bench/{{name}}")
            async def bench(name: str, n: int):
                return {{"secs": await workloads.WORKLOADS[name](n)}}
            """
        )
    )
    (root / "manage.py").write_text(
        textwrap.dedent(
            """
            import os, sys
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")
            from django.core.management import execute_from_command_line
            execute_from_command_line(sys.argv)
            """
        )
    )


def _get(path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=300) as response:
        return json.loads(response.read())


def _run_local(factory, name: str) -> float:
    loop = factory()
    try:
        return loop.run_until_complete(WORKLOADS[name](N))
    finally:
        loop.close()


def main() -> None:
    names = sys.argv[1:] or list(WORKLOADS)
    try:
        import uvloop  # noqa: PLC0415
    except ImportError:
        uvloop = None
        print("uvloop not installed: run with `uv run --with uvloop` for the comparison column")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_project(root)
        server = subprocess.Popen(
            [sys.executable, "manage.py", "runbolt", "--host", "127.0.0.1", "--port", str(PORT), "--processes", "1"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(200):
                try:
                    print("server loop:", _get("/health")["loop"])
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise SystemExit("runbolt did not come up")
            for name in names:
                columns = {"worker": lambda name=name: _get(f"/bench/{name}?n={N}")["secs"]}
                if uvloop is not None:
                    columns["uvloop"] = lambda name=name: _run_local(uvloop.new_event_loop, name)
                columns["stdlib"] = lambda name=name: _run_local(asyncio.new_event_loop, name)
                row = {label: min(run() for _ in range(REPS)) / N * 1e6 for label, run in columns.items()}
                line = f"{name:16s} " + "  ".join(f"{label}={value:7.2f}µs" for label, value in row.items())
                if "uvloop" in row:
                    line += f"   worker/uvloop={row['worker'] / row['uvloop']:.2f}x"
                print(line, flush=True)
        finally:
            server.terminate()
            server.wait()


if __name__ == "__main__":
    main()
