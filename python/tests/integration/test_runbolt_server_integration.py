from __future__ import annotations

import sys

import pytest

from .apps import app_source, app_text

pytestmark = pytest.mark.server_integration


def test_runbolt_autodiscovers_project_and_app_apis(make_server_project):
    project = make_server_project(
        installed_apps=["extraapp.apps.ExtraAppConfig"],
        api_source=app_source("autodiscover_project"),
        extra_files={
            "extraapp/__init__.py": "",
            "extraapp/apps.py": """
            from django.apps import AppConfig


            class ExtraAppConfig(AppConfig):
                name = "extraapp"
            """,
            "extraapp/api.py": app_text("autodiscover_extraapp"),
        },
    )

    with project.start() as server:
        project_response = server.get("/project-api")
        app_response = server.get("/app-api")

    assert project_response.status_code == 200
    assert project_response.json() == {"source": "project"}
    assert app_response.status_code == 200
    assert app_response.json() == {"source": "app"}


def test_runbolt_applies_global_cors_settings_at_startup(make_server_project):
    project = make_server_project(
        settings_extra="""
        CORS_ALLOWED_ORIGINS = ["https://example.com"]
        """,
        api_source=app_source("global_cors"),
    )

    with project.start() as server:
        response = server.get("/global-cors", headers={"Origin": "https://example.com"})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://example.com"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess smoke only runs on Linux.")
def test_runbolt_processes_two_shuts_down_cleanly(make_server_project):
    project = make_server_project(api_source=app_source("multiprocess_pid"))

    server = project.start(processes=2)
    response = server.get("/pid")
    stdout, stderr = server.stop()

    assert response.status_code == 200
    assert "pid" in response.json()
    assert server.process.returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
