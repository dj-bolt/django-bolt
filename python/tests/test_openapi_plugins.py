"""
Tests for OpenAPI UI render plugins.
"""

from django_bolt.openapi import plugins
from django_bolt.openapi.plugins import (
    JsonRenderPlugin,
    RapidocRenderPlugin,
    RedocRenderPlugin,
    ScalarRenderPlugin,
    StoplightRenderPlugin,
    SwaggerRenderPlugin,
    YamlRenderPlugin,
)

SCHEMA = {"openapi": "3.1.0", "info": {"title": "Test API", "version": "1.0.0"}, "paths": {}}
SCHEMA_URL = "/openapi.json"


def test_json_render_returns_schema_dict():
    """JSON plugin returns the schema dict unchanged."""
    assert JsonRenderPlugin().render(SCHEMA, SCHEMA_URL) is SCHEMA


def test_swagger_embeds_title_and_spec():
    """Swagger inlines the spec and shows the title."""
    html = SwaggerRenderPlugin().render(SCHEMA, SCHEMA_URL)
    assert "<title>Test API</title>" in html
    assert '"title":"Test API"' in html


def test_redoc_embeds_title_and_spec():
    """Redoc inlines the spec and shows the title."""
    html = RedocRenderPlugin().render(SCHEMA, SCHEMA_URL)
    assert "<title>Test API</title>" in html
    assert '"title":"Test API"' in html


def test_scalar_references_schema_url():
    """Scalar points at the schema URL via data-url."""
    html = ScalarRenderPlugin().render(SCHEMA, SCHEMA_URL)
    assert "Test API" in html
    assert f'data-url="{SCHEMA_URL}"' in html


def test_rapidoc_references_schema_url():
    """Rapidoc points at the schema URL via spec-url."""
    html = RapidocRenderPlugin().render(SCHEMA, SCHEMA_URL)
    assert "Test API" in html
    assert f'spec-url="{SCHEMA_URL}"' in html


def test_stoplight_references_schema_url():
    """Stoplight points at the schema URL via apiDescriptionUrl."""
    html = StoplightRenderPlugin().render(SCHEMA, SCHEMA_URL)
    assert "Test API" in html
    assert f'apiDescriptionUrl="{SCHEMA_URL}"' in html


def test_has_path():
    """has_path matches configured paths only."""
    plugin = JsonRenderPlugin(path="/custom.json")
    assert plugin.has_path("/custom.json") is True
    assert plugin.has_path("/nope.json") is False


def test_has_path_multiple():
    """Plugins with a list of default paths match each one."""
    yaml_plugin = YamlRenderPlugin()
    assert yaml_plugin.has_path("/openapi.yaml") is True
    assert yaml_plugin.has_path("/openapi.yml") is True

    scalar = ScalarRenderPlugin()
    assert scalar.has_path("/scalar") is True
    assert scalar.has_path("/") is True


def test_scalar_options_configuration():
    """Scalar options are encoded into the configuration script."""
    html = ScalarRenderPlugin(options={"theme": "purple"}).render(SCHEMA, SCHEMA_URL)
    assert 'dataset.configuration = \'{"theme":"purple"}\'' in html


def test_custom_js_and_css_urls():
    """Custom js_url/css_url override the CDN defaults in the output."""
    html = SwaggerRenderPlugin(js_url="https://x/ui.js", css_url="https://x/ui.css").render(SCHEMA, SCHEMA_URL)
    assert 'src="https://x/ui.js"' in html
    assert 'href="https://x/ui.css"' in html


def test_yaml_render():
    """YAML plugin dumps the schema as YAML when PyYAML is available."""
    out = YamlRenderPlugin().render(SCHEMA, SCHEMA_URL)
    assert "title: Test API" in out


def test_yaml_render_fallback(monkeypatch):
    """Without PyYAML the YAML plugin falls back to annotated JSON."""
    monkeypatch.setattr(plugins, "yaml", None)
    out = YamlRenderPlugin().render(SCHEMA, SCHEMA_URL)
    assert out.startswith("# PyYAML not installed")
    assert '"title":"Test API"' in out
