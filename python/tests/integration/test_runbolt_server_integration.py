from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from .apps import app_bytes, app_module, app_source
from .apps.loop_thread_probe import PROBE_MARKER
from .helpers import DEFAULT_HOST, _terminate_process

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
            "extraapp/api.py": app_bytes("autodiscover_extraapp"),
        },
    )

    with project.start() as server:
        project_response = server.get("/project-api")
        app_response = server.get("/app-api")
        app_health_response = server.get("/app-health")

    assert project_response.status_code == 200
    assert project_response.json() == {"source": "project"}
    assert app_response.status_code == 200
    assert app_response.json() == {"source": "app"}
    assert app_health_response.status_code == 200
    assert app_health_response.json() == {"status": "ok"}


def test_runbolt_applies_global_cors_settings_at_startup(make_server_project):
    project = make_server_project(
        settings_extra="""
        CORS_ALLOWED_ORIGINS = ["https://example.com"]
        """,
        api_module=app_module("global_cors"),
    )

    with project.start() as server:
        response = server.get("/global-cors", headers={"Origin": "https://example.com"})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://example.com"


def test_runbolt_prints_system_checks_and_migration_warning(make_server_project):
    """runbolt prints runserver-style startup diagnostics before the banner.

    The default test project installs contenttypes and auth on a fresh
    SQLite database, so unapplied migrations always exist.
    """
    project = make_server_project()

    server = project.start()
    stdout, _stderr = server.stop()

    assert "System check identified no issues" in stdout, stdout
    assert "unapplied migration" in stdout, stdout
    assert "Run 'python manage.py migrate' to apply them." in stdout, stdout
    # The checks run once, before the Bolt banner.
    assert stdout.count("unapplied migration") == 1, stdout
    assert stdout.index("unapplied migration") < stdout.index("Django Bolt"), stdout


def test_runbolt_skip_checks_suppresses_system_checks(make_server_project):
    project = make_server_project()

    server = project.start(extra_args=["--skip-checks"])
    stdout, _stderr = server.stop()

    assert "System check identified" not in stdout, stdout
    # The migration warning still prints, like runserver.
    assert "unapplied migration" in stdout, stdout


def test_runbolt_with_a_str_bolt_allowed_file_paths_stops_startup(make_server_project):
    # A str is a sequence of one-character paths, so "/" was an allowed dir, and FileResponse served any file.
    project = make_server_project(
        api_module=app_module("hello"), settings_extra='BOLT_ALLOWED_FILE_PATHS = "/srv/media"\n'
    )

    with pytest.raises(AssertionError) as excinfo, project.start():
        pass

    assert "runbolt exited before the server became ready" in str(excinfo.value)
    assert "BOLT_ALLOWED_FILE_PATHS must be a list of paths, got str '/srv/media'." in str(excinfo.value)


def test_runbolt_failing_system_check_stops_startup(make_server_project):
    project = make_server_project(
        installed_apps=["badapp.apps.BadAppConfig"],
        extra_files={
            "badapp/__init__.py": "",
            "badapp/apps.py": """
            from django.apps import AppConfig
            from django.core import checks


            class BadAppConfig(AppConfig):
                name = "badapp"

                def ready(self):
                    checks.register(self._fail)

                @staticmethod
                def _fail(app_configs, **kwargs):
                    return [checks.Error("intentional failing check", id="badapp.E001")]
            """,
        },
    )

    process, _port = project.spawn()
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        pytest.fail("runbolt kept running despite a failing system check")

    assert process.returncode != 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert "badapp.E001" in stderr, f"stdout:\n{stdout}\nstderr:\n{stderr}"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess smoke only runs on Linux.")
def test_runbolt_processes_two_shuts_down_cleanly(make_server_project):
    project = make_server_project(api_module=app_module("multiprocess_pid"))

    server = project.start(processes=2)
    response = server.get("/pid")
    stdout, stderr = server.stop()

    assert response.status_code == 200
    assert "pid" in response.json()
    assert server.process.returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"


def test_runbolt_bind_failure_exits_before_the_loop_thread_starts(make_server_project):
    """A port in use must stop startup before the event loop thread starts.

    If the thread starts first, the process can finalize Python before the
    thread attaches. The attach then panics, and the release build aborts with
    SIGABRT instead of exit code 1. ``runbolt --dev`` reads a signal on the
    first start as "keep watching", so the dev session did not exit.
    """
    project = make_server_project(api_module=app_module("loop_thread_probe"))

    # Hold the port without SO_REUSEPORT, so the bind of the server fails.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind((DEFAULT_HOST, 0))
        blocker.listen(8)
        port = int(blocker.getsockname()[1])

        process, _ = project.spawn(port=port, env={"BOLT_TEST_PROBE_LOOP_THREAD": "1"})
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            stdout, stderr = _terminate_process(process)
            pytest.fail(f"runbolt kept running after the bind of port {port} failed\nstderr:\n{stderr}")

    assert process.returncode == 1, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert "Address already in use" in stderr, stderr
    assert PROBE_MARKER not in stderr, stderr
