"""Test documentation middleware after runbolt merges multiple APIs."""

import httpx
import pytest

from .apps import app_module


@pytest.mark.server_integration
def test_merged_docs_debug_toolbar(make_server_project):
    pytest.importorskip("debug_toolbar")
    module = app_module("docs_middleware").split(":")[0]
    project = make_server_project(
        api_module=f"{module}:api",
        installed_apps=["django.contrib.staticfiles", "debug_toolbar"],
        urls_content='from django.urls import include, path\nurlpatterns = [path("__debug__/", include("debug_toolbar.urls"))]\n',
        templates=[{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}],
        settings_extra=f'''
BOLT_API = ["{module}:api", "{module}:other_api"]
INTERNAL_IPS = ["127.0.0.1"]
STATIC_URL = "/static/"
DEBUG_TOOLBAR_PANELS = ["debug_toolbar.panels.timer.TimerPanel"]
DEBUG_TOOLBAR_CONFIG = {{"RENDER_PANELS": True}}
''',
    )
    with project.start() as server:
        for route in ["/docs", "/docs/swagger"]:
            response = httpx.get(f"{server.base_url}{route}")
            assert response.status_code == 200, response.text
            assert 'id="djDebug"' in response.text
        response = httpx.get(f"{server.base_url}/docs/openapi.json")
        assert "/other/health" in response.json()["paths"]
        assert "Server-Timing" in response.headers
        response = httpx.get(f"{server.base_url}/other/health")
        assert response.status_code == 200
        assert "Server-Timing" not in response.headers
