"""A real runbolt serves a BoltAPI that the URLconf mounts under its prefix.

The startup wiring (autodiscovery, the URLconf walk, the merge) is what an
in-process TestClient cannot show.
"""

from __future__ import annotations

import pytest

from .apps import app_module

_URLS = """
from django.urls import path

from tests.integration.apps.urlconf_mount import missions

urlpatterns = [path("missions/", missions.urls)]
"""


@pytest.mark.server_integration
def test_runbolt_serves_a_mounted_api_under_its_prefix_one_time(make_server_project):
    module = app_module("urlconf_mount")
    project = make_server_project(
        api_module=module,
        urls_content=_URLS,
        # Autodiscovery also finds the mounted API. runbolt serves it one time, under its prefix.
        settings_extra=f"BOLT_API = [{module!r}, {module.replace(':api', ':missions')!r}]",
    )
    with project.start() as server:
        assert server.wait_for_json("/missions/42", lambda body: body == {"id": 42}) == {"id": 42}
        assert server.get("/42").status_code == 404
        assert server.get("/health").json() == {"status": "ok"}
