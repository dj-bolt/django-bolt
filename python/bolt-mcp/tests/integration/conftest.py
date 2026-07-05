"""Integration-test config: reuse django-bolt's subprocess server harness.

The harness (`create_server_project` + `RunningServer`) lives in the main repo's
`python/tests/integration/helpers.py`. We put the main repo's `python/`
directory on `sys.path`, import the harness as `tests.integration.helpers`, and
re-expose a `make_server_project` fixture. MCP integration tests spawn a real
`runbolt` server (the buffered in-process TestClient can't hold a live SSE
stream).
"""

from __future__ import annotations

import importlib
import sys
from itertools import count
from pathlib import Path

import pytest
from _helpers import mcp_app_source

_REPO_ROOT = Path(__file__).resolve().parents[4]
_REPO_PYTHON_DIR = _REPO_ROOT / "python"
if str(_REPO_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_PYTHON_DIR))

create_server_project = importlib.import_module("tests.integration.helpers").create_server_project


@pytest.fixture
def make_server_project(tmp_path_factory):
    counter = count()

    def factory(**kwargs):
        root = tmp_path_factory.mktemp(f"mcp_server_{next(counter)}")
        return create_server_project(root, **kwargs)

    return factory


@pytest.fixture(scope="module")
def feature_server(request, tmp_path_factory):
    """One started ``runbolt`` server shared by a whole test module.

    Reads the module's ``MCP_APP`` (and optional ``INSTALLED_APPS_EXTRA``) and
    starts a single process for the module, so feature tests that only issue
    read-only MCP calls can share it instead of paying a fresh server start
    each. Tests that mutate process/session state should use
    ``make_server_project`` for an isolated server instead.
    """
    root = tmp_path_factory.mktemp("mcp_feature_server")
    project = create_server_project(
        root,
        api_source=mcp_app_source(request.module.MCP_APP),
        installed_apps=getattr(request.module, "INSTALLED_APPS_EXTRA", None) or [],
    )
    with project.start() as server:
        yield server
