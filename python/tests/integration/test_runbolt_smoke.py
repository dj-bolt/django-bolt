from __future__ import annotations

import sys
import time

import pytest

pytestmark = [pytest.mark.server_integration, pytest.mark.platform_smoke]


def test_runbolt_serves_http_request(make_server_project):
    project = make_server_project(
        project_api_body="""
        @api.get("/hello")
        async def hello():
            return {"message": "plain"}
        """
    )

    with project.start() as server:
        response = server.get("/hello")

    assert response.status_code == 200
    assert response.json() == {"message": "plain"}


def test_runbolt_dev_serves_http_request(make_server_project):
    project = make_server_project(
        project_api_body="""
        @api.get("/hello")
        async def hello():
            return {"message": "dev"}
        """
    )

    with project.start(dev=True) as server:
        response = server.get("/hello")

    assert response.status_code == 200
    assert response.json() == {"message": "dev"}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Reload smoke runs only on Linux.")
def test_runbolt_dev_reloads_after_file_change(make_server_project):
    project = make_server_project(
        project_api_body="""
        @api.get("/version")
        async def version():
            return {"version": "v1"}
        """
    )

    with project.start(dev=True) as server:
        initial = server.get("/version")
        assert initial.json() == {"version": "v1"}

        time.sleep(0.3)
        project.write_project_api(
            """
            @api.get("/version")
            async def version():
                return {"version": "v2"}
            """
        )

        payload = server.wait_for_json("/version", lambda body: body["version"] == "v2", timeout=30)

    assert payload == {"version": "v2"}


def test_runbolt_dev_ignores_non_python_and_non_html_changes(make_server_project):
    project = make_server_project(
        project_api_body="""
        import os

        @api.get("/reload-state")
        async def reload_state():
            return {
                "pid": os.getpid(),
                "reload_count": int(os.environ.get("DJANGO_BOLT_DEV_RELOAD_COUNT", "0")),
            }
        """,
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
