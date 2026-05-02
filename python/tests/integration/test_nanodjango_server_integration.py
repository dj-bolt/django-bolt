"""End-to-end test: a single-file nanodjango app with the django-bolt plugin.

This catches integration regressions that unit tests can't: the plugin module
is loaded by nanodjango's pluggy plugin manager during ``Django()``
construction — *before* ``settings.configure()``. Any module-top import that
exposes Django's lazy ``settings`` proxy to pluggy's attribute walk triggers
``ImproperlyConfigured`` via ``inspect.isroutine`` → ``LazyObject.__class__``.

Without this test, that bug ships silently because the unit suite already has
Django configured by the time it imports django_bolt.nanodjango.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from .helpers import DEFAULT_HOST, RunningServer, _spawn_process, get_free_port

SINGLE_FILE_APP = """\
from __future__ import annotations

import sys

from nanodjango import Django

from django_bolt.nanodjango import BoltAPI

app = Django()
bolt = BoltAPI()


@app.route("/")
def home(request):
    return "<h1>Hello from Django</h1>"


@bolt.get("/api/hello")
async def hello():
    return {"message": "hello from bolt"}


@bolt.get("/api/greet")
async def greet(name: str):
    return {"message": f"hello, {name}!"}


bolt.mount_django(r"/")


if __name__ == "__main__":
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
"""


try:
    import nanodjango  # noqa: F401

    _has_nanodjango = True
except ImportError:
    _has_nanodjango = False


pytestmark = [
    pytest.mark.server_integration,
    pytest.mark.skipif(not _has_nanodjango, reason="nanodjango not installed"),
]


@pytest.fixture
def single_file_app(tmp_path: Path) -> Path:
    app_file = tmp_path / "myapp.py"
    app_file.write_text(textwrap.dedent(SINGLE_FILE_APP))
    return app_file


def test_single_file_app_serves_bolt_and_django_routes(single_file_app: Path):
    """End-to-end: pluggy loads the plugin, Django configures, both Bolt
    and Django routes serve correctly through a single port."""
    port = get_free_port()
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    process = _spawn_process(
        ["python", str(single_file_app), "runbolt", "--host", DEFAULT_HOST, "--port", str(port)],
        cwd=single_file_app.parent,
        env=env,
    )
    server = RunningServer(
        project=None,
        process=process,
        host=DEFAULT_HOST,
        port=port,
        startup_path="/api/hello",
    )
    try:
        # Bolt JSON route
        r = server.get("/api/hello")
        assert r.status_code == 200
        assert r.json() == {"message": "hello from bolt"}

        # Bolt route with query param coercion
        r = server.get("/api/greet", params={"name": "World"})
        assert r.status_code == 200
        assert r.json() == {"message": "hello, World!"}

        # Django catch-all (mount_django)
        r = server.get("/")
        assert r.status_code == 200
        assert "Hello from Django" in r.text

        # Bolt 404 (path under /api/* but unregistered)
        r = server.get("/api/missing")
        assert r.status_code == 404
    finally:
        _stdout, stderr = server.stop()
        # Surface plugin-load failures (the bug this test was written for):
        # ImproperlyConfigured happens at import-time of the app file, so it
        # shows up in stderr before the server ever binds.
        assert "ImproperlyConfigured" not in stderr, (
            f"plugin loaded with module-top Django settings access. stderr:\n{stderr}"
        )


def test_plugin_module_imports_without_django_configured(tmp_path: Path):
    """Importing ``django_bolt.nanodjango`` must not require Django settings.

    Pluggy walks module attributes during plugin discovery; any module-top
    import that exposes ``django.conf.settings`` (a lazy proxy) will fail
    with ``ImproperlyConfigured`` when pluggy probes attribute types.

    Run in a fresh subprocess so we don't observe a previously-configured
    Django from the parent test session.
    """
    if platform.system() == "Windows" and not shutil.which("python"):
        pytest.skip("python not on PATH")

    script = tmp_path / "probe.py"
    script.write_text(
        textwrap.dedent(
            """\
            # Deliberately do NOT call settings.configure(). If the plugin
            # module accesses settings at import time, this will raise.
            import django_bolt.nanodjango  # noqa: F401
            print("import-ok")
            """
        )
    )

    result = subprocess.run(
        ["python", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"importing django_bolt.nanodjango required Django settings to be configured. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "import-ok" in result.stdout
