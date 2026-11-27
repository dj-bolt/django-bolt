"""``runbolt`` takes the objects of the startup out of the garbage collector.

A full collection reads each object that the collector tracks, and the server
answers no request during that time. The objects of the startup live as long
as the process, so the collector does not need to read them.

Only a real server freezes its objects. ``TestClient`` does not: a test process
makes many apps, and a frozen object stays for the life of the process.
"""

from __future__ import annotations

import subprocess

import httpx
import pytest

from .apps import app_module
from .helpers import _terminate_process

pytestmark = pytest.mark.server_integration

# Django and Bolt make more objects than this number before the server starts.
MANY_OBJECTS = 10_000
# Python 3.12 starts with 375 frozen objects of its own. The other versions start with none.
FEW_OBJECTS = 1_000


def test_a_server_freezes_the_objects_of_its_startup(make_server_project):
    project = make_server_project(api_module=app_module("gc_freeze"))

    with project.start() as server:
        state = server.get("/collector").json()

    assert state["frozen"] > MANY_OBJECTS


def test_the_setting_off_leaves_each_object_to_the_collector(make_server_project):
    project = make_server_project(api_module=app_module("gc_freeze"), settings_extra="BOLT_GC_FREEZE = False\n")

    with project.start() as server:
        state = server.get("/collector").json()

    assert state["frozen"] < FEW_OBJECTS


def test_a_server_freezes_the_objects_that_its_lifespan_makes(make_server_project):
    """The freeze is after the startup of the lifespan, and not before it."""
    project = make_server_project(api_module=app_module("gc_freeze_lifespan"))

    with project.start() as server:
        state = server.get("/collector").json()

    assert state["frozen"] > MANY_OBJECTS
    assert state["collector_reads_the_startup_object"] is False


def test_each_worker_process_freezes_its_objects(make_server_project):
    project = make_server_project(api_module=app_module("gc_freeze"))

    with project.start(processes=2) as server:
        frozen_of_each_process: dict[int, int] = {}
        for _ in range(200):
            # A new connection for each request: the kernel gives each connection to one of the processes.
            state = httpx.get(f"{server.base_url}/collector", timeout=30).json()
            frozen_of_each_process[state["pid"]] = state["frozen"]
            if len(frozen_of_each_process) == 2:
                break

    assert len(frozen_of_each_process) == 2, frozen_of_each_process
    assert all(frozen > MANY_OBJECTS for frozen in frozen_of_each_process.values()), frozen_of_each_process


def test_a_setting_that_is_not_a_bool_stops_the_server_at_startup(make_server_project):
    project = make_server_project(api_module=app_module("gc_freeze"), settings_extra='BOLT_GC_FREEZE = "yes"\n')

    process, _port = project.spawn()
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        pytest.fail("runbolt kept running with a BOLT_GC_FREEZE that is not a bool")

    assert process.returncode != 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert "BOLT_GC_FREEZE must be a bool, got str 'yes'." in stderr, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    # The server refuses the setting before its startup: it prints no banner with its address.
    assert "127.0.0.1" not in stdout, f"stdout:\n{stdout}\nstderr:\n{stderr}"
