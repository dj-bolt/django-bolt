"""The runbolt supervisor stops its workers on SIGHUP and SIGQUIT (issue #910).

A terminal that closes, or an ssh session that ends, sends SIGHUP. Before, the
supervisor stopped and its workers kept serving on the port with no parent.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from .apps import app_module
from .helpers import child_pids


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.server_integration
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess runs only on Linux.")
@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGQUIT], ids=["SIGHUP", "SIGQUIT"])
def test_supervisor_stops_its_workers_on_signal(make_server_project, signum):
    project = make_server_project(api_module=app_module("multiprocess_pid"))
    with project.start(processes=2) as server:
        workers = child_pids(server.process.pid)
        assert len(workers) == 2, workers

        os.kill(server.process.pid, signum)
        deadline = time.time() + 15.0
        while server.process.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        exit_code = server.process.poll()
        # Look at the workers before stop(), which signals the whole process group.
        orphans = {pid for pid in workers if _alive(pid)}
        stdout, stderr = server.stop()

    assert orphans == set(), f"workers still run after the supervisor stopped\nstderr:\n{stderr}"
    assert exit_code == 0, f"exit code {exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}"


@pytest.mark.server_integration
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess runs only on Linux.")
def test_worker_does_not_keep_the_sighup_handler_of_the_supervisor(make_server_project):
    """SIGHUP to one worker stops that worker, as the default action does.

    A forked worker gets the signal handlers of the supervisor. If it kept the
    SIGHUP handler, it would ignore the signal.
    """
    project = make_server_project(api_module=app_module("multiprocess_pid"))
    with project.start(processes=2) as server:
        worker = min(child_pids(server.process.pid))

        os.kill(worker, signal.SIGHUP)
        deadline = time.time() + 15.0
        while worker in child_pids(server.process.pid) and time.time() < deadline:
            time.sleep(0.1)
        still_running = worker in child_pids(server.process.pid)
        stdout, stderr = server.stop()

    assert not still_running, f"worker {worker} ignored SIGHUP\nstderr:\n{stderr}"
