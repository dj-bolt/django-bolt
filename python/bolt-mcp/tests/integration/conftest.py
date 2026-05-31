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
