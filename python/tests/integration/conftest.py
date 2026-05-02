from __future__ import annotations

import os
from itertools import count
from pathlib import Path

import pytest

from .helpers import create_server_project


@pytest.fixture
def make_server_project(tmp_path_factory):
    project_counter = count()

    def factory(**kwargs):
        project_root = tmp_path_factory.mktemp(f"server_project_{next(project_counter)}")
        return create_server_project(project_root, **kwargs)

    return factory


@pytest.fixture(scope="module")
def artifact_path() -> Path:
    artifact = os.environ.get("DJANGO_BOLT_ARTIFACT_PATH")
    if not artifact:
        pytest.skip("Set DJANGO_BOLT_ARTIFACT_PATH to run artifact smoke tests.")
    path = Path(artifact)
    if not path.exists():
        pytest.skip(f"Artifact path does not exist: {path}")
    return path
