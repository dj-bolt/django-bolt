"""Regression tests for schema rendering and Django middleware on docs."""

import sys

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import include, path

from django_bolt import BoltAPI
from django_bolt.openapi import (
    JsonRenderPlugin,
    OpenAPIConfig,
    ScalarRenderPlugin,
    SwaggerRenderPlugin,
    YamlRenderPlugin,
)
from django_bolt.testing import TestClient


@pytest.mark.parametrize("plugin_path", ["/openapi.json", "/schema.json"])
def test_explicit_json_plugin(plugin_path):
    api = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Explicit JSON", version="1", render_plugins=[JsonRenderPlugin(path=plugin_path)]
        )
    )
    api._register_openapi_routes()
    with TestClient(api) as client:
        for url in ["/docs", f"/docs{plugin_path}", "/docs/openapi.json"]:
            response = client.get(url)
            assert response.status_code == 200, response.text
            assert response.headers["content-type"] == "application/vnd.oai.openapi+json"
            assert response.json()["info"]["title"] == "Explicit JSON"


def test_explicit_yaml_plugin():
    api = BoltAPI(openapi_config=OpenAPIConfig(title="YAML", version="1", render_plugins=[YamlRenderPlugin()]))
    api._register_openapi_routes()
    with TestClient(api) as client:
        for url in ["/docs", "/docs/openapi.yaml", "/docs/openapi.yml"]:
            response = client.get(url)
            assert response.status_code == 200, response.text
            assert response.headers["content-type"] == "text/yaml; charset=utf-8"
            assert "title: YAML" in response.text


@pytest.mark.parametrize("plugin", [ScalarRenderPlugin(), SwaggerRenderPlugin()])
def test_debug_toolbar_in_docs(plugin, monkeypatch):
    pytest.importorskip("debug_toolbar")
    with override_settings(
        INSTALLED_APPS=[*settings.INSTALLED_APPS, "debug_toolbar"],
        ROOT_URLCONF=__name__,
        DEBUG_TOOLBAR_PANELS=["debug_toolbar.panels.timer.TimerPanel"],
        DEBUG_TOOLBAR_CONFIG={"SHOW_TOOLBAR_CALLBACK": lambda _request: True, "RENDER_PANELS": True},
    ):
        monkeypatch.setattr(
            sys.modules[__name__],
            "urlpatterns",
            [path("__debug__/", include("debug_toolbar.urls"))],
            raising=False,
        )
        api = BoltAPI(
            django_middleware=["debug_toolbar.middleware.DebugToolbarMiddleware"],
            openapi_config=OpenAPIConfig(title="Toolbar", version="1", render_plugins=[plugin]),
        )
        api._register_openapi_routes()
        with TestClient(api) as client:
            for url in ["/docs", f"/docs{plugin.paths[0]}"]:
                response = client.get(url)
                assert response.status_code == 200, response.text
                assert 'id="djDebug"' in response.text
                assert response.text.index('id="djDebug"') < response.text.lower().rindex("</body>")
            response = client.get("/docs/openapi.json")
            assert response.status_code == 200
            assert response.json()["info"]["title"] == "Toolbar"
            assert 'id="djDebug"' not in response.text
