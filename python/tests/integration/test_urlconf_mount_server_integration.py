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
        # reverse() in a handler gives the served path.
        assert server.get("/link").json() == {"url": "/missions/42"}


_PREFIXED_REVERSE_ONLY_URLS = """
from django.urls import include, path

urlpatterns = [path("api/", include("django_bolt.urls"))]
"""

_TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True}]


@pytest.mark.server_integration
def test_runbolt_serves_the_apis_of_django_bolt_urls_under_its_prefix(make_server_project):
    project = make_server_project(
        api_module=app_module("url_reverse"), urls_content=_PREFIXED_REVERSE_ONLY_URLS, templates=_TEMPLATES
    )
    with project.start(startup_path="/api/health") as server:
        # The {% url %} tag gives the served path.
        assert server.wait_for_text("/api/render", 'href="/api/missions/42"') == '<a href="/api/missions/42">go</a>'
        assert server.get("/api/missions/42").json() == {"id": 42}
        assert server.get("/missions/42").status_code == 404


@pytest.mark.server_integration
def test_runbolt_under_force_script_name_serves_the_stripped_path_and_reverses_with_the_prefix(make_server_project):
    module = app_module("urlconf_mount")
    project = make_server_project(
        api_module=module,
        urls_content=_URLS,
        # The proxy removes /myapp from the path, as for Django's WSGIHandler.
        settings_extra='FORCE_SCRIPT_NAME = "/myapp"',
    )
    with project.start() as server:
        assert server.wait_for_json("/missions/42", lambda body: body == {"id": 42}) == {"id": 42}
        assert server.get("/link").json() == {"url": "/myapp/missions/42"}
        assert server.get("/myapp/missions/42").status_code == 404
