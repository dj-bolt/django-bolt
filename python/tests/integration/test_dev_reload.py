from __future__ import annotations

import sys
import time

import pytest

from .apps import app_source

pytestmark = [pytest.mark.server_integration]


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
