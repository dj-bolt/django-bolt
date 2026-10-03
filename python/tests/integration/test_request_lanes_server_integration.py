"""Request lanes on a real ``runbolt`` server.

The production handler sends a sync request flow to a Rust lane thread and
awaits the result on the Tokio runtime. ``TestClient`` blocks on the lane
with a different call, so only a real server covers the production path and
the lane shutdown.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from .apps import app_module


@pytest.mark.server_integration
def test_thread_local_middleware_state_stays_with_its_request(make_server_project):
    project = make_server_project(api_module=app_module("request_lanes"))

    with project.start() as server:
        # One client for each thread: a shared httpx.Client races on a free-threaded build.
        clients = threading.local()

        def fetch(index: int) -> tuple[dict, dict]:
            if not hasattr(clients, "client"):
                clients.client = httpx.Client(timeout=10)
            tenant = f"t{index}"
            return (
                clients.client.get(server.url(f"/sync/{tenant}")).json(),
                clients.client.get(server.url(f"/async/{tenant}")).json(),
            )

        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(fetch, range(200)))

    for sync_body, async_body in results:
        assert sync_body["actual"] == sync_body["expected"]
        assert sync_body["on_lane"] is True
        assert async_body["actual"] == async_body["expected"]


@pytest.mark.server_integration
def test_server_with_live_lanes_stops_cleanly(make_server_project):
    project = make_server_project(api_module=app_module("request_lanes"))

    server = project.start()
    server.wait_until_ready()
    assert server.get("/sync/acme").json()["on_lane"] is True

    stdout, stderr = server.stop()

    assert server.process.returncode == 0, stderr
    assert "panicked" not in stderr


@pytest.mark.server_integration
def test_idle_lane_closes_its_connections_and_stops(make_server_project):
    project = make_server_project(api_module=app_module("request_lanes"))

    with project.start(env={"DJANGO_BOLT_LANE_IDLE_SECONDS": "1"}) as server:
        # Two requests in the idle time use one lane and one connection.
        assert server.get("/db").json() == {"value": 1}
        assert server.get("/db").json() == {"value": 1}
        assert server.get("/connections").json() == {"open": 1}

        # Each request resets the idle time of the lane that it uses, so wait with no traffic.
        time.sleep(2.5)

        assert server.get("/connections").json() == {"open": 0}
        # A request after the stop gets a new lane and a new connection.
        assert server.get("/db").json() == {"value": 1}
        assert server.get("/connections").json() == {"open": 1}


@pytest.mark.server_integration
def test_async_request_does_not_reuse_an_expired_lane_connection(make_server_project):
    project = make_server_project(
        api_module=app_module("request_lanes"),
        settings_extra='DATABASES["default"]["CONN_MAX_AGE"] = 60',
    )

    with project.start() as server:
        # Each request expires the connection of its lane, so the next one must reconnect.
        assert server.get("/async-expire").json() == {"created": 1}
        assert server.get("/async-expire").json() == {"created": 2}


@pytest.mark.server_integration
def test_async_request_drops_a_broken_lane_connection(make_server_project):
    project = make_server_project(api_module=app_module("request_lanes"))

    with project.start() as server:
        assert server.get("/async-break").json() == {"broken": True}
        # This request finds the broken connection. The lane drops it at the request end.
        assert server.get("/async-db").status_code == 500
        assert server.get("/async-db").json() == {"value": 1}


@pytest.mark.server_integration
def test_lane_checks_a_healthy_connection_one_time_for_each_request(make_server_project):
    project = make_server_project(
        api_module=app_module("request_lanes_async"),
        settings_extra='DATABASES["default"]["CONN_HEALTH_CHECKS"] = True\nDATABASES["default"]["CONN_MAX_AGE"] = 60',
    )

    with project.start() as server:
        server.get("/health-checks")
        # The middleware query does the one health check, before the handler starts.
        assert server.get("/health-checks").json() == {"checks": 0}


@pytest.mark.server_integration
def test_sync_request_drops_a_broken_connection_after_a_handled_error(make_server_project):
    project = make_server_project(api_module=app_module("request_lanes"))

    with project.start() as server:
        assert server.get("/sync-break-handled").json() == {"handled": True}
        assert server.get("/db").json() == {"value": 1}
