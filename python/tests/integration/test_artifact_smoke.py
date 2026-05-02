from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.artifact_smoke


def _venv_python(venv_dir: Path) -> Path:
    if platform.system() == "Windows":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


@pytest.fixture(scope="module")
def installed_artifact_python(tmp_path_factory, artifact_path: Path) -> Path:
    venv_dir = tmp_path_factory.mktemp("artifact_venv")
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    python = _venv_python(venv_dir)
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(python), "-m", "pip", "install", str(artifact_path)], check=True)
    return python


def test_installed_artifact_runs_runbolt(make_server_project, installed_artifact_python: Path):
    project = make_server_project(
        python_executable=str(installed_artifact_python),
        preserve_pythonpath=False,
        project_api_body="""
        @api.get("/artifact")
        async def artifact():
            return {"mode": "plain"}
        """,
    )

    with project.start() as server:
        response = server.get("/artifact")

    assert response.status_code == 200
    assert response.json() == {"mode": "plain"}


def test_installed_artifact_runs_runbolt_dev(make_server_project, installed_artifact_python: Path):
    project = make_server_project(
        python_executable=str(installed_artifact_python),
        preserve_pythonpath=False,
        project_api_body="""
        @api.get("/artifact")
        async def artifact():
            return {"mode": "dev"}
        """,
    )

    with project.start(dev=True) as server:
        response = server.get("/artifact")

    assert response.status_code == 200
    assert response.json() == {"mode": "dev"}
