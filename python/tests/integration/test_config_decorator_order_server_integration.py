"""A config decorator above the route decorator stops ``runbolt``.

``TestClient`` has the same check (``python/tests/test_decorator_syntax.py``).
This test proves that the ``runbolt`` startup calls it too.
"""

from __future__ import annotations

import pytest

from .apps import app_source

pytestmark = pytest.mark.server_integration


def test_a_config_decorator_above_the_route_decorator_stops_runbolt(make_server_project):
    project = make_server_project(api_source=app_source("config_decorator_above_route"))

    with pytest.raises(AssertionError) as excinfo, project.start(timeout=15):
        pass
    assert "@rate_limit is above the route decorator of GET /limited, so it has no effect" in str(excinfo.value)
