from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from .apps import app_bytes, app_module, app_source
from .apps.loop_thread_probe import PROBE_MARKER
from .helpers import DEFAULT_HOST, _terminate_process, get_free_port

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


def test_runbolt_runs_the_lifespan_of_a_mounted_api(make_server_project):
    project = make_server_project(api_module=app_module("mounted_lifespan"))

    with project.start(startup_path="/health") as server:
        response = server.get("/child/state")

    assert response.status_code == 200
    assert response.json() == {"started": True}


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
    assert "BOLT_ALLOWED_FILE_PATHS must be a list of absolute paths, got str '/srv/media'." in str(excinfo.value)


def test_runbolt_with_a_bolt_compression_of_a_wrong_type_stops_startup(make_server_project):
    project = make_server_project(api_module=app_module("hello"), settings_extra="BOLT_COMPRESSION = True\n")

    with pytest.raises(AssertionError) as excinfo, project.start():
        pass

    assert "runbolt exited before the server became ready" in str(excinfo.value)
    assert "settings.BOLT_COMPRESSION must be a CompressionConfig, None or False, not bool" in str(excinfo.value)


def test_runbolt_with_two_websocket_routes_at_one_position_stops_with_a_command_error(make_server_project):
    project = make_server_project(api_module=app_module("websocket_route_conflict"))

    with pytest.raises(AssertionError) as excinfo, project.start(timeout=15):
        pass

    assert (
        "CommandError: Failed to register route: WEBSOCKET /live/{rest:path} conflicts with WEBSOCKET /live/{room}."
    ) in str(excinfo.value)


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


def test_runbolt_stops_when_the_host_has_no_address(make_server_project):
    """A --host that does not resolve must stop startup.

    Before, the server bound 0.0.0.0 for each host that was not an IP address.
    Thus it answered on all interfaces with no error.
    """
    project = make_server_project(api_module=app_module("hello"))

    # RFC 6761: a name under .invalid never resolves.
    host = "bolt-test.invalid"
    process, port = project.spawn(host=host, port=get_free_port())
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_process(process)
        pytest.fail(f"runbolt kept running with --host {host} on port {port}\nstderr:\n{stderr}")

    assert process.returncode == 1, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert f'cannot resolve host "{host}"' in stderr, stderr


@pytest.mark.parametrize(
    "processes",
    [
        1,
        pytest.param(
            2,
            marks=pytest.mark.skipif(
                not sys.platform.startswith("linux"), reason="Multiprocess smoke only runs on Linux."
            ),
        ),
    ],
)
def test_runbolt_with_no_bolt_api_stops_with_an_error(make_server_project, processes):
    """A project with no BoltAPI has nothing to serve. runbolt must not report success."""
    project = make_server_project()
    project.write_file(f"{project.package_name}/api.py", "NOT_AN_API = 1\n")

    process, port = project.spawn(processes=processes)
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_process(process)
        pytest.fail(f"runbolt kept running with no BoltAPI on port {port}\nstderr:\n{stderr}")

    assert process.returncode == 1, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert "CommandError: No BoltAPI instances found." in stderr, stderr
    # The supervisor must not start workers that have nothing to serve.
    assert "exited unexpectedly" not in stdout + stderr, f"stdout:\n{stdout}\nstderr:\n{stderr}"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess smoke only runs on Linux.")
def test_runbolt_processes_two_bind_failure_stops_with_exit_status_1(make_server_project):
    """No worker can bind the port. The supervisor must stop and report the failure."""
    project = make_server_project(api_module=app_module("hello"))

    # Hold the port without SO_REUSEPORT, so the bind of each worker fails.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind((DEFAULT_HOST, 0))
        blocker.listen(8)
        port = int(blocker.getsockname()[1])

        process, _ = project.spawn(port=port, processes=2)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            stdout, stderr = _terminate_process(process)
            pytest.fail(f"runbolt kept running after the bind of port {port} failed\nstderr:\n{stderr}")

    assert process.returncode == 1, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert stderr.count("Address already in use") == 2, stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Multiprocess smoke only runs on Linux.")
def test_runbolt_respawn_failed_workers_stops_when_no_worker_can_start(make_server_project):
    """--respawn-failed-workers must not replace a worker that did not start to serve.

    Before, runbolt started a new worker each second and did not stop.
    """
    project = make_server_project(api_module=app_module("hello"))

    # Hold the port without SO_REUSEPORT, so the bind of each worker fails.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind((DEFAULT_HOST, 0))
        blocker.listen(8)
        port = int(blocker.getsockname()[1])

        process, _ = project.spawn(port=port, processes=2, extra_args=["--respawn-failed-workers"])
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            stdout, stderr = _terminate_process(process)
            pytest.fail(
                f"runbolt kept running after the bind of port {port} failed\n"
                f"respawns: {stdout.count('respawning')}\nstdout:\n{stdout[-1500:]}"
            )

    assert process.returncode == 1, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert "Address already in use" in stderr, stderr
    assert "failed to start" in stdout, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert "respawning" not in stdout, f"stdout:\n{stdout}\nstderr:\n{stderr}"
