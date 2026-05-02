from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.server_integration


def test_runbolt_autodiscovers_project_and_app_apis(make_server_project):
    project = make_server_project(
        installed_apps=["extraapp.apps.ExtraAppConfig"],
        project_api_body="""
        @api.get("/project-api")
        async def project_api():
            return {"source": "project"}
        """,
        extra_files={
            "extraapp/__init__.py": "",
            "extraapp/apps.py": """
            from django.apps import AppConfig


            class ExtraAppConfig(AppConfig):
                name = "extraapp"
            """,
            "extraapp/api.py": """
            from django_bolt import BoltAPI

            api = BoltAPI()


            @api.get("/app-api")
            async def app_api():
                return {"source": "app"}
            """,
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
        project_api_body="""
        @api.get("/global-cors")
        async def global_cors():
            return {"ok": True}
        """,
    )

    with project.start() as server:
        response = server.get("/global-cors", headers={"Origin": "https://example.com"})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://example.com"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess smoke only runs on Linux.")
def test_runbolt_processes_two_shuts_down_cleanly(make_server_project):
    project = make_server_project(
        project_api_body="""
        import os


        @api.get("/pid")
        async def pid():
            return {"pid": os.getpid()}
        """
    )

    server = project.start(processes=2)
    response = server.get("/pid")
    stdout, stderr = server.stop()

    assert response.status_code == 200
    assert "pid" in response.json()
    assert server.process.returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
