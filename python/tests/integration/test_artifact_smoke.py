from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

import pytest

from .apps import app_source

pytestmark = pytest.mark.artifact_smoke

# psycopg_pool 3.2.8 is the last release before 3.3. It has no ConnectionPool.drain().
PSYCOPG_POOL_WITHOUT_DRAIN = "psycopg-pool==3.2.8"

# These settings add a second database that uses Django's connection pool. No
# server serves this database. Startup checks the pool of each database, but it
# connects only to the default one, for the migration check.
POOLED_DATABASE = """
DATABASES["pooled"] = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "unused",
    "HOST": "127.0.0.1",
    "PORT": "1",
    "OPTIONS": {"pool": True},
}
"""


def _venv_python(venv_dir: Path) -> Path:
    if platform.system() == "Windows":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _artifact_requirement(artifact_path: Path, extra: str = "") -> str:
    return f"django-bolt{extra} @ {artifact_path.resolve().as_uri()}"


def _venv_with(venv_dir: Path, *requirements: str) -> Path:
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    python = _venv_python(venv_dir)
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(python), "-m", "pip", "install", *requirements], check=True)
    return python


@pytest.fixture(scope="module")
def installed_artifact_python(tmp_path_factory, artifact_path: Path) -> Path:
    return _venv_with(tmp_path_factory.mktemp("artifact_venv"), str(artifact_path))


@pytest.fixture(scope="module")
def pool_extra_python(tmp_path_factory, artifact_path: Path) -> Path:
    return _venv_with(tmp_path_factory.mktemp("pool_extra_venv"), _artifact_requirement(artifact_path, "[pool]"))


@pytest.fixture(scope="module")
def pool_without_drain_python(tmp_path_factory, artifact_path: Path) -> Path:
    return _venv_with(
        tmp_path_factory.mktemp("pool_without_drain_venv"),
        str(artifact_path),
        "psycopg>=3.3",
        PSYCOPG_POOL_WITHOUT_DRAIN,
    )


def test_installed_artifact_runs_runbolt(make_server_project, installed_artifact_python: Path):
    project = make_server_project(
        python_executable=str(installed_artifact_python),
        preserve_pythonpath=False,
        api_source=app_source("artifact"),
    )

    with project.start() as server:
        response = server.get("/artifact")

    assert response.status_code == 200
    assert response.json() == {"mode": "ok"}


def test_installed_artifact_runs_runbolt_dev(make_server_project, installed_artifact_python: Path):
    project = make_server_project(
        python_executable=str(installed_artifact_python),
        preserve_pythonpath=False,
        api_source=app_source("artifact"),
    )

    with project.start(dev=True) as server:
        response = server.get("/artifact")

    assert response.status_code == 200
    assert response.json() == {"mode": "ok"}


def test_the_pool_extra_installs_a_pool_that_runbolt_accepts(make_server_project, pool_extra_python: Path):
    """Install the ``pool`` extra, and start a server with Django's connection pool."""
    project = make_server_project(
        python_executable=str(pool_extra_python),
        preserve_pythonpath=False,
        api_source=app_source("artifact"),
        settings_extra=POOLED_DATABASE,
    )

    with project.start() as server:
        response = server.get("/artifact")

    assert response.status_code == 200
    assert response.json() == {"mode": "ok"}


def test_the_pool_extra_refuses_a_pool_without_drain(installed_artifact_python: Path, artifact_path: Path):
    """The ``pool`` extra and psycopg_pool 3.2 cannot install together."""
    result = subprocess.run(
        [
            str(installed_artifact_python),
            "-m",
            "pip",
            "install",
            "--dry-run",
            _artifact_requirement(artifact_path, "[pool]"),
            PSYCOPG_POOL_WITHOUT_DRAIN,
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, result.stdout
    assert "psycopg-pool>=3.3" in result.stdout + result.stderr


def test_runbolt_warns_on_a_pool_without_drain(make_server_project, pool_without_drain_python: Path):
    """Without the ``pool`` extra, psycopg_pool 3.2 can stay. The server runs, and a startup warning names the extra.

    Bolt then cannot drain the pool: after a database restart, each dead idle
    connection of the pool fails one request.
    """
    project = make_server_project(
        python_executable=str(pool_without_drain_python),
        preserve_pythonpath=False,
        api_source=app_source("artifact"),
        settings_extra=POOLED_DATABASE,
    )

    server = project.start()
    with server:
        assert server.get("/health").status_code == 200
    stdout, stderr = server.stop()

    output = stdout + stderr
    assert "psycopg_pool before 3.3" in output, output
    assert 'Install "django-bolt[pool]"' in output, output
