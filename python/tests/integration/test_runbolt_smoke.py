from __future__ import annotations

import sys
import time

import pytest

from .apps import app_source

pytestmark = [pytest.mark.server_integration, pytest.mark.platform_smoke]


def test_runbolt_serves_http_request(make_server_project):
    project = make_server_project(api_source=app_source("hello"))

    with project.start() as server:
        response = server.get("/hello")

    assert response.status_code == 200
    assert response.json() == {"message": "hello"}


def test_runbolt_dev_serves_http_request(make_server_project):
    project = make_server_project(api_source=app_source("hello"))

    with project.start(dev=True) as server:
        response = server.get("/hello")

    assert response.status_code == 200
    assert response.json() == {"message": "hello"}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Reload smoke runs only on Linux.")
def test_runbolt_dev_reloads_after_file_change(make_server_project):
    project = make_server_project(api_source=app_source("reload_v1"))

    with project.start(dev=True) as server:
        initial = server.get("/version")
        assert initial.json() == {"version": "v1"}

        time.sleep(0.3)
        project.install_api(app_source("reload_v2"))

        payload = server.wait_for_json("/version", lambda body: body["version"] == "v2", timeout=30)

    assert payload == {"version": "v2"}


def test_runbolt_dev_ignores_non_python_and_non_html_changes(make_server_project):
    project = make_server_project(
        api_source=app_source("reload_state"),
        extra_files={"testproj/runtime.log": "initial\n"},
    )

    with project.start(dev=True) as server:
        initial = server.get("/reload-state").json()
        assert initial["reload_count"] == 0

        time.sleep(0.3)
        project.write_file("testproj/runtime.log", "updated\n")
        time.sleep(1.0)

        after = server.get("/reload-state").json()

    assert after["reload_count"] == initial["reload_count"]
    assert after["pid"] == initial["pid"]
