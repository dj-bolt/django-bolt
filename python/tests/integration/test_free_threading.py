"""Free-threaded CPython (PEP 703) support.

Three guarantees:

- Importing ``django_bolt._core`` must not re-enable the GIL on a free-threaded
  interpreter (the module attests thread safety with ``gil_used = false``).
- Dispatch stays correct when many threads drive the Rust pipeline at once.
  Under the GIL this is a plain concurrency test; on a free-threaded build the
  handlers really run in parallel.
- ``runbolt`` defaults to one Actix worker thread per CPU on a free-threaded
  interpreter (``--workers`` overrides) and reports the interpreter mode.

The in-process tests run on every build. The subprocess tests need a real
``runbolt`` because ``TestClient`` never reads ``--workers``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from django_bolt.management.commands.runbolt import default_worker_threads
from django_bolt.testing import TestClient

from .apps import app_module
from .apps import free_threading as free_threading_app
from .helpers import generate_load

FREE_THREADED_BUILD = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
GIL_DISABLED = FREE_THREADED_BUILD and not sys._is_gil_enabled()

ROUTES = [("/sync/{n}", "sync"), ("/trivial/{n}", "trivial"), ("/awaiting/{n}", "awaiting")]


@pytest.mark.skipif(not FREE_THREADED_BUILD, reason="needs a free-threaded CPython build")
def test_extension_import_keeps_gil_disabled():
    # A fresh interpreter: the GIL state is decided when the extension is
    # imported. CPython warns (RuntimeWarning) when a module re-enables the
    # GIL; -W error turns that warning into a failed import.
    code = "import sys, django_bolt._core; print(sys._is_gil_enabled())"
    result = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", result.stdout


def test_default_worker_threads_follows_gil_state():
    if GIL_DISABLED:
        assert default_worker_threads() == (os.cpu_count() or 1)
    else:
        assert default_worker_threads() == 1


def test_parallel_dispatch_from_many_threads():
    """Every dispatch shape stays correct under concurrent client threads."""
    per_thread = 30

    with TestClient(free_threading_app.api) as client:

        def worker(worker_id: int) -> int:
            for i in range(per_thread):
                n = worker_id * 1000 + i
                path, kind = ROUTES[i % len(ROUTES)]
                response = client.get(path.format(n=n))
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["n"] == n
                assert body["kind"] == kind
                if kind == "awaiting":
                    assert body["parts"] == [0, 2, 4]
                stream = client.get(f"/stream/{n}")
                assert stream.status_code == 200
                assert stream.text == f"{n}:0\n{n}:1\n{n}:2\n"
            return worker_id

        with ThreadPoolExecutor(max_workers=16) as pool:
            assert sorted(pool.map(worker, range(16))) == list(range(16))

        assert client.get("/gil").json() == free_threading_app.gil_state()


@pytest.mark.server_integration
def test_real_server_keeps_interpreter_gil_state_under_load(make_server_project):
    """The server process reports the same GIL state as the interpreter that started it.

    On a free-threaded build this proves the extension did not re-enable the
    GIL. The load phase drives sync, trivially-async, and awaiting routes from
    16 client threads at once.
    """
    # The harness reads logs at shutdown. Keep successful requests out of that pipe.
    project = make_server_project(
        api_module=app_module("free_threading"),
        settings_extra="""
        LOGGING = {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {"django.server": {"level": "WARNING"}},
        }
        """,
    )

    with project.start() as server:
        assert server.get("/gil").json() == {
            "free_threaded_build": FREE_THREADED_BUILD,
            "gil_enabled": not GIL_DISABLED,
        }

        for path, kind in ROUTES:
            result = generate_load(server.url(path.format(n=42)), duration_s=1.0, concurrency=16)
            assert result.failed == 0, (kind, result)
            assert result.ok > 0, (kind, result)

        response = server.get("/awaiting/42")
        assert response.json() == {"n": 42, "kind": "awaiting", "parts": [0, 2, 4]}


@pytest.mark.server_integration
def test_runbolt_workers_option_sets_actix_threads(make_server_project):
    project = make_server_project(api_module=app_module("free_threading"))

    server = project.start(extra_args=["--workers", "3"])
    try:
        for n in range(20):
            assert server.get(f"/sync/{n}").json() == {"n": n, "kind": "sync"}
    finally:
        stdout, _stderr = server.stop()

    assert "3 threads each" in stdout, stdout
    assert ("free-threaded" in stdout) is GIL_DISABLED, stdout


@pytest.mark.server_integration
def test_async_handlers_stay_on_their_worker_thread(make_server_project):
    """Each Actix worker thread owns one WorkerLoop.

    An async handler resumes on the thread that accepted its request, so
    ``threading.local`` state (Django DB connections) survives an await, and
    two worker threads never run callbacks of one asyncio loop at once.
    """
    project = make_server_project(api_module=app_module("free_threading"))
    requests_per_thread = 8

    with project.start(extra_args=["--workers", "2"]) as server:

        def worker(worker_id: int) -> list[dict]:
            bodies = []
            with httpx.Client(timeout=30) as client:
                for i in range(requests_per_thread):
                    n = worker_id * 100 + i
                    response = client.get(server.url(f"/thread/{n}"))
                    assert response.status_code == 200, response.text
                    bodies.append(response.json())
            return bodies

        with ThreadPoolExecutor(max_workers=16) as pool:
            bodies = [body for batch in pool.map(worker, range(16)) for body in batch]

    moved = [body for body in bodies if body["thread_before"] != body["thread_after"]]
    assert not moved, moved[:3]
    loops_by_thread = {body["thread_before"]: body["loop"] for body in bodies}
    # 16 keep-alive connections reach both Actix workers.
    assert len(loops_by_thread) == 2, loops_by_thread
    assert len(set(loops_by_thread.values())) == 2, loops_by_thread


@pytest.mark.server_integration
def test_runbolt_default_workers_follow_interpreter(make_server_project):
    project = make_server_project(api_module=app_module("free_threading"))

    server = project.start()
    stdout, _stderr = server.stop()

    expected = default_worker_threads()
    if expected == 1:
        assert "threads each" not in stdout, stdout
    else:
        assert f"{expected} threads each" in stdout, stdout


@pytest.mark.server_integration
def test_runbolt_rejects_zero_workers(make_server_project):
    project = make_server_project(api_module=app_module("free_threading"))

    process, _port = project.spawn(extra_args=["--workers", "0"])
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0
    assert "--workers must be at least 1" in stderr, stdout + stderr
