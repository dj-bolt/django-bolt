"""Integration-test config: reuse django-bolt's subprocess server harness.

The harness (`create_server_project` + `RunningServer`) lives in the main repo's
`python/tests/integration/helpers.py`. We load it by file path (it is a
standalone module) to avoid a package-name clash with this local `integration`
test package, and re-expose a `make_server_project` fixture. MCP integration
tests spawn a real `runbolt` server (the buffered in-process TestClient can't
hold a live SSE stream).
"""

from __future__ import annotations

import importlib.util
import sys
from itertools import count
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_HELPERS_PATH = _REPO_ROOT / "python" / "tests" / "integration" / "helpers.py"

_spec = importlib.util.spec_from_file_location("_dbolt_mcp_it_helpers", _HELPERS_PATH)
_helpers = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _helpers  # dataclass forward-ref resolution needs this
_spec.loader.exec_module(_helpers)
create_server_project = _helpers.create_server_project


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

    Reads the module's ``MCP_API_BODY`` (and optional ``INSTALLED_APPS_EXTRA``) and starts a
    single process for the module, so feature tests that only issue read-only MCP calls can
    share it instead of paying a fresh server start each. Tests that mutate process/session
    state should use ``make_server_project`` for an isolated server instead.
    """
    root = tmp_path_factory.mktemp("mcp_feature_server")
    project = create_server_project(
        root,
        project_api_body=request.module.MCP_API_BODY,
        installed_apps=getattr(request.module, "INSTALLED_APPS_EXTRA", None) or [],
    )
    with project.start() as server:
        yield server
