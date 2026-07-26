from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from .apps import app_bytes, app_source
from .helpers import DEFAULT_HOST, RunningServer, ServerProject, _spawn_process, _terminate_process, get_free_port

pytestmark = [pytest.mark.server_integration]


def _move_project_into_src_layout(project: ServerProject) -> str:
    """Rearrange a generated project into a "src layout" and return its ``-m`` name.

    Turns::

        root/manage.py
        root/testproj/{settings,urls,api}.py

    into the layout from the report, where manage.py lives *inside* the package
    it imports from::

        root/src/__init__.py
        root/src/manage.py            # DJANGO_SETTINGS_MODULE=src.testproj.settings
        root/src/api.py               # where autodiscovery looks for "src.*" URLConfs
        root/src/testproj/{settings,urls}.py

    Only ``python -m src.manage`` can start this: as a script, manage.py's own
    directory (``root/src``) heads ``sys.path`` and ``import src.*`` fails.
    """
    package = project.package_name
    source = project.read_file(f"{package}/settings.py")
    project.write_file(
        f"src/{package}/settings.py",
        source.replace(f'"{package}.urls"', f'"src.{package}.urls"'),
    )
    for name in ("__init__.py", "urls.py"):
        project.write_file(f"src/{package}/{name}", project.path(f"{package}/{name}").read_bytes())
        project.path(f"{package}/{name}").unlink()
    # ROOT_URLCONF "src.testproj.urls" makes "src.api" the project API module.
    project.write_file("src/api.py", project.path(f"{package}/api.py").read_bytes())
    project.path(f"{package}/api.py").unlink()
    project.path(f"{package}/settings.py").unlink()
    project.path(package).rmdir()
    project.path("manage.py").unlink()

    project.write_file("src/__init__.py", "")
    project.write_file(
        "src/manage.py",
        f"""
        import os
        import sys

        if __name__ == "__main__":
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "src.{package}.settings")
            from django.core.management import execute_from_command_line

            execute_from_command_line(sys.argv)
        """,
    )
    return "src.manage"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Reload integration runs only on Linux.")
def test_runbolt_dev_reloads_when_imported_helper_changes(make_server_project):
    """Editing a module imported BY api.py must trigger a reload.

    The dev supervisor never imports the api module itself, so before the
    priming-import fix its watch list covered only django.setup()'s import
    graph plus the api.py file — the api module's own imports were invisible
    to the watcher.
    """
    project = make_server_project(
        api_source=app_source("reload_import_dep"),
        extra_files={"reload_helper.py": app_bytes("_reload_helper")},
    )

    with project.start(dev=True) as server:
        initial = server.get("/helper-version")
        assert initial.json() == {"version": "helper-v1"}

        time.sleep(0.3)
        project.write_file("reload_helper.py", app_bytes("_reload_helper_v2"))

        payload = server.wait_for_json("/helper-version", lambda body: body["version"] == "helper-v2", timeout=30)

    assert payload == {"version": "helper-v2"}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Reload integration runs only on Linux.")
def test_runbolt_dev_reloads_when_new_module_created_in_project_root(make_server_project):
    """Creating a brand-new .py file anywhere under the project root must
    trigger a reload — the root is watched recursively by default
    (uvicorn-style), not just the files already in the import graph."""
    project = make_server_project(api_source=app_source("reload_state"))

    with project.start(dev=True) as server:
        initial = server.get("/reload-state").json()
        assert initial["reload_count"] == 0

        time.sleep(0.3)
        project.write_file("newfeature/helpers.py", "VALUE = 1\n")

        after = server.wait_for_json("/reload-state", lambda body: body["reload_count"] == 1, timeout=30)

    assert after["pid"] != initial["pid"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Reload integration runs only on Linux.")
def test_runbolt_dev_reloads_for_reload_dir_entry(make_server_project):
    """A .py change inside a --reload-dir directory OUTSIDE the project root
    must trigger a reload even though the api module never imports it — the
    project root itself is already watched by default, so the flag exists for
    exactly these out-of-root locations."""
    project = make_server_project(api_source=app_source("reload_state"))
    external_plugins = project.root.parent / f"{project.root.name}_plugins"
    external_plugins.mkdir()
    (external_plugins / "tasks.py").write_text("TASK = 1\n")

    with project.start(dev=True, extra_args=["--reload-dir", str(external_plugins)]) as server:
        initial = server.get("/reload-state").json()
        assert initial["reload_count"] == 0

        time.sleep(0.3)
        (external_plugins / "tasks.py").write_text("TASK = 2\n")

        after = server.wait_for_json("/reload-state", lambda body: body["reload_count"] == 1, timeout=30)

    assert after["pid"] != initial["pid"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Reload integration runs only on Linux.")
def test_runbolt_dev_serves_and_reloads_for_module_invocation(make_server_project):
    """``python -m src.manage runbolt --dev`` must work in a src-layout project.

    Regression test for issue #271: the dev supervisor rebuilt the worker command
    from ``sys.argv`` alone, but CPython rewrites ``argv[0]`` to manage.py's
    resolved path for ``-m`` runs. The worker was therefore respawned as a plain
    script, which puts ``src/`` at the head of ``sys.path`` instead of the cwd —
    so ``src.testproj.settings`` became unimportable and the worker died with
    ``ModuleNotFoundError: No module named 'src'`` (preceded by a misleading
    ``KeyError: 'runbolt'``). Only ``--dev`` was affected.
    """
    project = make_server_project(api_source=app_source("reload_v1"))
    main_module = _move_project_into_src_layout(project)

    # No PYTHONPATH: the project root is importable only because ``-m`` puts the
    # cwd on sys.path. That is precisely what a script-mode respawn loses.
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    port = get_free_port()
    process = _spawn_process(
        [
            sys.executable,
            "-m",
            main_module,
            "runbolt",
            "--dev",
            "--host",
            DEFAULT_HOST,
            "--port",
            str(port),
        ],
        cwd=project.root,
        env=env,
    )
    server = RunningServer(
        project=None,
        process=process,
        host=DEFAULT_HOST,
        port=port,
        startup_path="/version",
    )

    with server:
        assert server.get("/version").json() == {"version": "v1"}

        # The respawned worker must keep the -m context too, so reloads survive.
        time.sleep(0.3)
        project.write_file("src/api.py", app_bytes("reload_v2"))

        payload = server.wait_for_json("/version", lambda body: body["version"] == "v2", timeout=30)

    assert payload == {"version": "v2"}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Reload integration runs only on Linux.")
def test_runbolt_dev_exits_when_first_start_fails(make_server_project):
    """A failed FIRST start must exit, not sit in "waiting for changes".

    The supervisor's wait-for-changes behaviour exists so a SyntaxError in the
    app doesn't tear down the dev session. But a startup failure like "address
    already in use" is not fixable by editing a file, so waiting there leaves
    the developer with a live-looking process that will never serve, and a zero
    exit status. The first worker's exit code must propagate instead — the same
    protocol Django's runserver uses (only its reload signal, exit code 3,
    respawns; anything else exits).
    """
    project = make_server_project(api_source=app_source("reload_state"))

    # Hold the port WITHOUT SO_REUSEPORT so the worker's bind really fails
    # (dev mode is single-process and does not set DJANGO_BOLT_REUSE_PORT).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind((DEFAULT_HOST, 0))
        blocker.listen(8)
        port = int(blocker.getsockname()[1])

        process, _ = project.spawn(dev=True, port=port)
        try:
            stdout, stderr = process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            stdout, stderr = _terminate_process(process)
            pytest.fail(
                f"runbolt --dev kept running after the first worker failed to bind port {port}"
                f"\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )

    assert process.returncode == 1, f"expected the worker's exit code to propagate\nstderr:\n{stderr}"
    assert "Address already in use" in stderr, stderr
    assert "Waiting for changes" not in stderr, stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Reload integration runs only on Linux.")
def test_runbolt_dev_keeps_watching_when_a_reloaded_worker_crashes(make_server_project):
    """After a successful start, a crashing worker must NOT kill the session.

    This is the case the wait-for-changes branch is for: the developer saves a
    broken file, the worker dies on import, and the supervisor stays alive so
    the next save recovers it. The first-start exit must not regress this.
    """
    project = make_server_project(api_source=app_source("reload_v1"))

    with project.start(dev=True) as server:
        assert server.get("/version").json() == {"version": "v1"}

        time.sleep(0.3)
        project.write_file(f"{project.package_name}/api.py", "raise RuntimeError('broken app')\n")

        # Wait until the crashed worker stops answering. server.get() raises if
        # the supervisor itself died — exactly the regression guarded here.
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                server.get("/version")
            except httpx.HTTPError:
                break
            time.sleep(0.2)
        else:
            pytest.fail("the broken app kept serving — the reload never happened")

        assert server.process.poll() is None, "dev supervisor exited when a reloaded worker crashed"

        project.install_api(app_source("reload_v2"))
        payload = server.wait_for_json("/version", lambda body: body["version"] == "v2", timeout=30)

    assert payload == {"version": "v2"}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Reload integration runs only on Linux.")
def test_runbolt_dev_reloads_with_force_polling(make_server_project):
    """The PollWatcher backend must trigger reloads just like RecommendedWatcher.

    Polling can lag the inotify path by up to one poll interval (500ms), so
    use the same generous timeout as the inotify case.
    """
    project = make_server_project(
        api_source=app_source("reload_v1"),
        settings_extra="BOLT_DEV_FORCE_POLLING = True",
    )

    with project.start(dev=True) as server:
        initial = server.get("/version")
        assert initial.json() == {"version": "v1"}

        time.sleep(0.3)
        project.install_api(app_source("reload_v2"))

        payload = server.wait_for_json("/version", lambda body: body["version"] == "v2", timeout=30)

    assert payload == {"version": "v2"}
