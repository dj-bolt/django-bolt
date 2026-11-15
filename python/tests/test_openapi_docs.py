"""
Tests for OpenAPI documentation generation and rendering.

These tests verify that the OpenAPI/Swagger documentation endpoints
are working correctly and not throwing internal errors.
"""

import re
import time
from typing import Annotated
from urllib.parse import urljoin

import jwt
import msgspec
import pytest
import yaml
from django.contrib.auth import alogin
from django.contrib.auth.models import User
from django.test import override_settings
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate

from django_bolt import BoltAPI, Request
from django_bolt.auth import (
    AllowAny,
    APIKeyAuthentication,
    IsAuthenticated,
    JWTAuthentication,
    Requires,
    SessionAuthentication,
)
from django_bolt.datastructures import UploadFile
from django_bolt.error_handlers import ProblemDetails, problem_details
from django_bolt.openapi import (
    OpenAPIConfig,
    RapidocRenderPlugin,
    RedocRenderPlugin,
    ScalarRenderPlugin,
    StoplightRenderPlugin,
    SwaggerRenderPlugin,
)
from django_bolt.openapi.spec import (
    Components,
    Contact,
    ExternalDocumentation,
    License,
    SecurityScheme,
    Server,
    Tag,
)
from django_bolt.params import File, Form
from django_bolt.serializers import Serializer, field
from django_bolt.testing import TestClient
from django_bolt.websocket.types import WebSocket


class Item(msgspec.Struct):
    """Model for OpenAPI schema generation."""

    id: int
    name: str
    price: float
    is_active: bool | None = None


def test_openapi_json_endpoint():
    """Test that /docs/openapi.json returns valid JSON without errors."""
    # Create API with OpenAPI enabled (default path is /docs)
    api = BoltAPI(
        openapi_config=OpenAPIConfig(title="Test API", version="1.0.0", description="Test API for OpenAPI docs")
    )

    # Add some test routes with various parameter types
    @api.get("/items/{item_id}")
    async def get_item(item_id: int, q: str | None = None):
        """Get an item by ID."""
        return {"item_id": item_id, "q": q}

    @api.post("/items", response_model=Item)
    async def create_item(item: Item) -> Item:
        """Create a new item."""
        return item

    # Test the OpenAPI JSON endpoint
    # Note: Must register OpenAPI routes BEFORE creating TestClient
    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")

        # Should return 200 OK (not 500 Internal Server Error)
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

        # Should return valid JSON
        data = response.json()
        assert data is not None

        # Verify basic OpenAPI structure
        assert "openapi" in data
        assert "info" in data
        assert data["info"]["title"] == "Test API"
        assert data["info"]["version"] == "1.0.0"
        assert "paths" in data

        # Verify our routes are in the schema
        assert "/items/{item_id}" in data["paths"]
        assert "/items" in data["paths"]


@pytest.mark.parametrize(
    ("names", "title"),
    [
        ({"PROJECT_NAME": "Shop", "SITE_NAME": "Site"}, "Shop"),
        ({"SITE_NAME": "Site"}, "Site"),
        ({}, "API"),
    ],
    ids=["project-name", "site-name", "no-name"],
)
def test_default_openapi_config_takes_the_title_from_the_settings(settings, names, title):
    """With no ``openapi_config``, the title is PROJECT_NAME, then SITE_NAME, then "API"."""
    for name, value in names.items():
        setattr(settings, name, value)

    api = BoltAPI()

    @api.get("/items")
    async def items():
        return []

    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"] == {"title": title, "version": "1.0.0"}


def test_swagger_ui_endpoint():
    """Test that /docs/swagger (Swagger UI) loads without internal errors."""
    # Create API with Swagger UI enabled (default path is /docs)
    api = BoltAPI(
        openapi_config=OpenAPIConfig(title="Test API", version="1.0.0", render_plugins=[SwaggerRenderPlugin()])
    )

    # Add a simple route
    @api.get("/test")
    async def test_endpoint():
        """Test endpoint."""
        return {"status": "ok"}

    # Test the Swagger UI endpoint
    # Note: Must register OpenAPI routes BEFORE creating TestClient
    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get("/docs/swagger")

        # Should return 200 OK (not 500 Internal Server Error)
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

        # Should return HTML content
        assert response.headers.get("content-type", "").startswith("text/html")

        # Should contain Swagger UI indicators
        html = response.text
        assert "swagger" in html.lower() or "openapi" in html.lower()


@pytest.mark.parametrize(
    ("options", "expected_url"),
    [
        ({}, "https://cdn.jsdelivr.net/npm/redoc@2.5.3/bundles/redoc.standalone.js"),
        ({"version": "2.5.1"}, "https://cdn.jsdelivr.net/npm/redoc@2.5.1/bundles/redoc.standalone.js"),
        ({"version": "2.5.1", "js_url": "/static/redoc.js"}, "/static/redoc.js"),
    ],
)
def test_redoc_endpoint_uses_available_bundle(options, expected_url):
    """Use a published bundle by default and preserve explicit URL overrides."""
    api = BoltAPI(
        openapi_config=OpenAPIConfig(title="ReDoc API", version="1.0.0", render_plugins=[RedocRenderPlugin(**options)])
    )

    @api.get("/items")
    async def get_items():
        return []

    api._register_openapi_routes()
    with TestClient(api) as client:
        response = client.get("/docs/redoc/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert f'<script src="{expected_url}" crossorigin></script>' in response.text
    assert "Redoc.init(" in response.text
    assert '"/items"' in response.text


def test_docs_pages_escape_the_text_of_the_api():
    """The title, a docstring and the Scalar options are text. They do not end a tag or a script of the page."""
    api = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Tom & <b>Co</b> API",
            version="1.0.0",
            render_plugins=[
                SwaggerRenderPlugin(path="/swagger"),
                RedocRenderPlugin(path="/redoc"),
                ScalarRenderPlugin(path="/scalar", options={"title": "Tom's API"}),
                RapidocRenderPlugin(path="/rapidoc"),
                StoplightRenderPlugin(path="/stoplight"),
            ],
        )
    )

    @api.get("/items")
    async def get_items():
        """Give the items as </script><script>alert(1)</script> shows them."""
        return []

    api._register_openapi_routes()
    with TestClient(api) as client:
        pages = {
            name: client.get(f"/docs/{name}").text for name in ("swagger", "redoc", "scalar", "rapidoc", "stoplight")
        }
        description = client.get("/docs/openapi.json").json()["paths"]["/items"]["get"]["summary"]

    assert description == "Give the items as </script><script>alert(1)</script> shows them."
    for name, page in pages.items():
        assert "<title>Tom &amp; &lt;b&gt;Co&lt;/b&gt; API</title>" in page, name
        assert "<script>alert(1)</script>" not in page, name
    # The page holds the docstring as JSON text that the browser reads back to the same string.
    for name in ("swagger", "redoc"):
        assert r"\u003c/script\u003e\u003cscript\u003ealert(1)\u003c/script\u003e" in pages[name], name
    # The apostrophe does not end a JavaScript string.
    assert """dataset.configuration = JSON.stringify({"title":"Tom's API"})""" in pages["scalar"]


@pytest.mark.parametrize(
    ("plugin", "url", "expected_parts"),
    [
        (
            RapidocRenderPlugin(),
            "/docs/rapidoc",
            [
                "<title>Docs UI API</title>",
                '<script src="https://unpkg.com/rapidoc@9.3.4/dist/rapidoc-min.js" crossorigin></script>',
                '<rapi-doc spec-url="/docs/openapi.json" />',
            ],
        ),
        (
            StoplightRenderPlugin(),
            "/docs/elements",
            [
                "<title>Docs UI API</title>",
                '<link rel="stylesheet" href="https://unpkg.com/@stoplight/elements@7.7.18/styles.min.css">',
                '<script src="https://unpkg.com/@stoplight/elements@7.7.18/web-components.min.js" crossorigin></script>',
                'apiDescriptionUrl="/docs/openapi.json"',
                'router="hash"',
                'layout="sidebar"',
            ],
        ),
    ],
)
def test_rapidoc_and_stoplight_endpoints_render_the_page(plugin, url, expected_parts):
    """Each UI page names the API title, its bundle and the schema URL."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Docs UI API", version="1.0.0", render_plugins=[plugin]))
    api._register_openapi_routes()
    with TestClient(api) as client:
        response = client.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text.lstrip().startswith("<!DOCTYPE html>")
    for part in expected_parts:
        assert part in response.text
    assert plugin.favicon in response.text
    assert plugin.style in response.text
    assert '<meta charset="utf-8"/>' in response.text
    assert 'name="viewport"' in response.text


_FAVICON = '<link rel="icon" href="/static/docs.ico">'
_STYLE = "<style>body { margin: 4px }</style>"


@pytest.mark.parametrize(
    ("plugin_class", "options", "url", "present", "absent"),
    [
        (
            SwaggerRenderPlugin,
            {"version": "5.40.0"},
            "/docs/swagger",
            [
                '<link href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.40.0/swagger-ui.css" rel="stylesheet">',
                '<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.40.0/swagger-ui-bundle.js" crossorigin>',
                '<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.40.0/swagger-ui-standalone-preset.js"',
            ],
            [],
        ),
        (
            SwaggerRenderPlugin,
            {"js_url": "/static/ui.js", "css_url": "/static/ui.css", "standalone_preset_js_url": "/static/preset.js"},
            "/docs/swagger",
            [
                '<link href="/static/ui.css" rel="stylesheet">',
                '<script src="/static/ui.js" crossorigin>',
                '<script src="/static/preset.js" crossorigin>',
            ],
            ["cdn.jsdelivr.net"],
        ),
        (RedocRenderPlugin, {}, "/docs/redoc", ['<link href="https://fonts.googleapis.com/css?family='], []),
        (RedocRenderPlugin, {"google_fonts": False}, "/docs/redoc", ["Redoc.init("], ["fonts.googleapis.com"]),
        (
            ScalarRenderPlugin,
            {"version": "1.25.0"},
            "/docs/scalar",
            [
                '<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.25.0" crossorigin>',
                f'<link rel="stylesheet" type="text/css" href="{ScalarRenderPlugin._default_css_url}">',
                'data-url="/docs/openapi.json"',
            ],
            ["dataset.configuration"],
        ),
        (
            ScalarRenderPlugin,
            {"js_url": "/static/scalar.js", "css_url": "/static/scalar.css", "options": {"theme": "moon"}},
            "/docs/scalar",
            [
                '<script src="/static/scalar.js" crossorigin>',
                '<link rel="stylesheet" type="text/css" href="/static/scalar.css">',
                "dataset.configuration = ",
                '{"theme":"moon"}',
            ],
            ["cdn.jsdelivr.net"],
        ),
        (
            RapidocRenderPlugin,
            {"version": "9.3.8"},
            "/docs/rapidoc",
            ['<script src="https://unpkg.com/rapidoc@9.3.8/dist/rapidoc-min.js" crossorigin>'],
            [],
        ),
        (
            RapidocRenderPlugin,
            {"js_url": "/static/rapidoc.js"},
            "/docs/rapidoc",
            ['<script src="/static/rapidoc.js" crossorigin>'],
            ["unpkg.com"],
        ),
        (
            StoplightRenderPlugin,
            {"version": "8.0.0"},
            "/docs/elements",
            [
                '<link rel="stylesheet" href="https://unpkg.com/@stoplight/elements@8.0.0/styles.min.css">',
                '<script src="https://unpkg.com/@stoplight/elements@8.0.0/web-components.min.js" crossorigin>',
            ],
            [],
        ),
        (
            StoplightRenderPlugin,
            {"js_url": "/static/elements.js", "css_url": "/static/elements.css"},
            "/docs/elements",
            ['<link rel="stylesheet" href="/static/elements.css">', '<script src="/static/elements.js" crossorigin>'],
            ["unpkg.com"],
        ),
    ],
    ids=[
        "swagger-version",
        "swagger-urls",
        "redoc-fonts",
        "redoc-no-fonts",
        "scalar-version",
        "scalar-urls-options",
        "rapidoc-version",
        "rapidoc-url",
        "stoplight-version",
        "stoplight-urls",
    ],
)
def test_docs_page_options_change_the_page(plugin_class, options, url, present, absent):
    """A UI plugin serves its page at its default path, with the options of its constructor."""
    plugin = plugin_class(favicon=_FAVICON, style=_STYLE, **options)
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Options API", version="1.0.0", render_plugins=[plugin]))
    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    page = response.text
    assert "<title>Options API</title>" in page
    assert _FAVICON in page
    assert _STYLE in page
    for part in present:
        assert part in page
    for part in absent:
        assert part not in page


def test_openapi_root_serves_the_plugin_with_the_root_path():
    """The docs root serves the plugin that has the "/" path. With no such plugin, it serves the first plugin."""
    scalar_last = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Root API", version="1.0.0", render_plugins=[RedocRenderPlugin(), ScalarRenderPlugin()]
        )
    )
    no_root = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Root API", version="1.0.0", render_plugins=[RapidocRenderPlugin(), RedocRenderPlugin()]
        )
    )
    scalar_last._register_openapi_routes()
    no_root._register_openapi_routes()

    with TestClient(scalar_last) as client:
        assert 'id="api-reference"' in client.get("/docs").text
        assert "Redoc.init(" in client.get("/docs/redoc").text
    with TestClient(no_root) as client:
        assert "<rapi-doc" in client.get("/docs").text


def test_swagger_ui_supports_openapi_3_2_query_specs():
    """Regression: the default Swagger UI must understand schemas containing QUERY.

    QUERY is an OpenAPI 3.2 operation. Swagger UI only gained OpenAPI 3.2
    support in 5.32.0; older bundles render an "invalid version field" error
    even though the generated document itself is valid.
    """
    api = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Query API",
            version="1.0.0",
            render_plugins=[SwaggerRenderPlugin(path="/")],
        )
    )

    @api.query("/search")
    async def search():
        return {"results": []}

    api._register_openapi_routes()

    with TestClient(api) as client:
        schema_response = client.get("/docs/openapi.json")
        docs_response = client.get("/docs")

    assert schema_response.status_code == 200
    assert schema_response.json()["openapi"] == "3.2.0"
    assert docs_response.status_code == 200
    assert '"openapi":"3.2.0"' in docs_response.text

    bundle_version = re.search(r"swagger-ui-dist@(\d+)\.(\d+)\.(\d+)/swagger-ui-bundle", docs_response.text)
    assert bundle_version is not None
    assert tuple(map(int, bundle_version.groups())) >= (5, 32, 0)


def test_openapi_root_path_serves_ui_directly():
    """Test that /docs serves default UI directly without redirect loop.

    This test catches the bug where:
    - /docs redirected to /docs/
    - NormalizePath::trim() stripped trailing slash back to /docs
    - Infinite redirect loop

    The fix serves the default UI directly at /docs instead of redirecting.

    We verify this by checking that response.history is empty (no redirects occurred).
    """
    api = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Test API", version="1.0.0", path="/docs", render_plugins=[SwaggerRenderPlugin(path="/")]
        )
    )

    @api.get("/test")
    async def test_endpoint():
        return {"status": "ok"}

    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get("/docs")

        # CRITICAL: Must NOT redirect - this is what causes the infinite loop
        # in production with NormalizePath::trim() middleware.
        # TestClient doesn't have NormalizePath, so redirect would "work" here,
        # but in production: /docs -> redirect /docs/ -> trim to /docs -> loop
        assert len(response.history) == 0, (
            f"/docs should serve UI directly without redirect, but got redirects: {response.history}"
        )

        # Should return 200 with HTML content directly
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"

        # Should be HTML content (Swagger UI)
        content_type = response.headers.get("content-type", "")
        assert "text/html" in content_type, f"Expected HTML, got {content_type}"

        # Should contain Swagger UI content
        html = response.text
        assert "swagger" in html.lower(), "Response should contain Swagger UI"


@pytest.mark.parametrize(
    ("config_path", "plugin_path"),
    [("docs", "scalar"), ("/docs/", "scalar/"), ("/docs", "/scalar")],
)
def test_docs_paths_with_no_leading_slash_serve_a_page_that_loads_its_schema(config_path, plugin_path):
    """The docs path and the path of a UI get a leading slash. The page gives a schema URL that answers."""
    api = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Test API",
            version="1.0.0",
            path=config_path,
            render_plugins=[ScalarRenderPlugin(path=plugin_path)],
        )
    )

    @api.get("/test")
    async def test_endpoint():
        return {"status": "ok"}

    api._register_openapi_routes()

    with TestClient(api) as client:
        page = client.get("/docs/scalar")
        assert page.status_code == 200, page.text
        assert client.get("/docsscalar").status_code == 404

        # Resolve the schema URL of the page as a browser does.
        schema_url = re.search(r'data-url="([^"]+)"', page.text).group(1)
        schema = client.get(urljoin("http://testserver/docs/scalar", schema_url))
        assert schema.status_code == 200, schema_url
        assert "/test" in schema.json()["paths"]


def test_openapi_renders_serializer_with_field_marker_default():
    """Regression: serializer field() used as default must not leak the
    internal _FieldMarker (which carries a bare object() sentinel for
    unset defaults) into the OpenAPI schema's JSON output.
    """

    class AstronautCreated(Serializer):
        id: int
        name: str
        mission: str = field(source="mission.name")

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.post("/astronauts", response_model=AstronautCreated)
    async def create(a: AstronautCreated) -> AstronautCreated:
        return a

    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get("/docs")
        assert response.status_code == 200, response.text

        response = client.get("/docs/openapi.json")
        assert response.status_code == 200, response.text

        schema = response.json()["components"]["schemas"]["AstronautCreated"]
        assert "default" not in schema["properties"]["mission"]
        assert "mission" in schema["required"]


def test_openapi_disabled_returns_404():
    """Test that disabled OpenAPI docs return 404."""
    api = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Test API",
            version="1.0.0",
            enabled=False,  # Explicitly disabled
        )
    )

    @api.get("/test")
    async def test_endpoint():
        return {"status": "ok"}

    # Register routes (should skip OpenAPI routes due to enabled=False)
    api._register_openapi_routes()

    with TestClient(api) as client:
        # All doc routes should return 404
        response = client.get("/docs/openapi.json")
        assert response.status_code == 404, f"Disabled docs should return 404, got {response.status_code}"

        response = client.get("/docs")
        assert response.status_code == 404, f"Disabled docs UI should return 404, got {response.status_code}"


def test_openapi_exclude_paths_match_whole_path_segments():
    """`/admin` hides `/admin` and `/admin/users`. It does not hide `/administrators`."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0", exclude_paths=["/admin", "/ws"]))

    async def handler():
        return {}

    for path in ("/admin", "/admin/users", "/administrators", "/docs-archive", "/visible"):
        api.get(path)(handler)

    @api.websocket("/ws/internal")
    async def internal_socket(websocket: WebSocket):
        await websocket.accept()

    @api.websocket("/wsx")
    async def public_socket(websocket: WebSocket):
        await websocket.accept()

    api._register_openapi_routes()

    with TestClient(api) as client:
        paths = client.get("/docs/openapi.json").json()["paths"]
        # The routes are not documented, but the server serves them.
        assert client.get("/admin/users").status_code == 200

    assert sorted(paths) == ["/administrators", "/docs-archive", "/visible", "/wsx"]


def test_openapi_protected_without_auth_returns_401():
    """Test that protected docs without authentication return 401."""
    api = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Test API",
            version="1.0.0",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAuthenticated()],
        )
    )

    @api.get("/test")
    async def test_endpoint():
        return {"status": "ok"}

    api._register_openapi_routes()

    with TestClient(api) as client:
        # Without token, should get 401
        response = client.get("/docs/openapi.json")
        assert response.status_code == 401, f"Protected docs without auth should return 401, got {response.status_code}"

        response = client.get("/docs")
        assert response.status_code == 401, (
            f"Protected docs UI without auth should return 401, got {response.status_code}"
        )


def test_openapi_protected_with_valid_auth():
    """Test that protected docs with valid authentication work."""
    api = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Test API",
            version="1.0.0",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAuthenticated()],
        )
    )

    @api.get("/test")
    async def test_endpoint():
        return {"status": "ok"}

    api._register_openapi_routes()

    # Create valid token
    token = jwt.encode({"sub": "user123", "exp": int(time.time()) + 3600}, "test-secret", algorithm="HS256")

    with TestClient(api) as client:
        headers = {"Authorization": f"Bearer {token}"}

        # With valid token, should get 200
        response = client.get("/docs/openapi.json", headers=headers)
        assert response.status_code == 200, (
            f"Protected docs with valid auth should return 200, got {response.status_code}"
        )

        # Verify it returns valid JSON schema
        data = response.json()
        assert "openapi" in data
        assert data["info"]["title"] == "Test API"

        # UI should also work
        response = client.get("/docs", headers=headers)
        assert response.status_code == 200, (
            f"Protected docs UI with valid auth should return 200, got {response.status_code}"
        )


def test_openapi_all_routes_protected():
    """Test that all OpenAPI routes are protected when guards are set."""
    api = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Test API",
            version="1.0.0",
            path="/docs",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAuthenticated()],
            render_plugins=[SwaggerRenderPlugin()],
        )
    )

    @api.get("/test")
    async def test_endpoint():
        return {"status": "ok"}

    api._register_openapi_routes()

    with TestClient(api) as client:
        # All routes should return 401 without auth
        routes_to_test = [
            "/docs/openapi.json",
            "/docs/openapi.yaml",
            "/docs/openapi.yml",
            "/docs",  # Root UI
            "/docs/swagger",  # Swagger UI
        ]

        for route in routes_to_test:
            response = client.get(route)
            assert response.status_code == 401, f"Route {route} should be protected (401), got {response.status_code}"


def _docs_api_with_login(**config) -> BoltAPI:
    """Docs protected by the session backend, plus a route that logs a user in."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0", **config))

    @api.post("/login", auth=[SessionAuthentication()], guards=[AllowAny()])
    async def login(request: Request, username: str):
        await alogin(request, await User.objects.aget(username=username))
        return {"ok": True}

    api._register_openapi_routes()
    return api


def test_openapi_yaml_endpoints_serve_the_same_document_as_json():
    """Both YAML paths return 200, a YAML media type and the document of the JSON path."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def list_items() -> list[Item]:
        return []

    api._register_openapi_routes()
    with TestClient(api) as client:
        document = client.get("/docs/openapi.json").json()
        for path in ("/docs/openapi.yaml", "/docs/openapi.yml"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-type"] == "text/yaml; charset=utf-8", path
            assert yaml.safe_load(response.text) == document, path


def test_openapi_config_fields_reach_the_document():
    """Info, servers, security and external docs of the config appear in the document."""
    config = OpenAPIConfig(
        title="Test API",
        version="2.1.0",
        description="Long text.",
        summary="Short text.",
        terms_of_service="https://example.com/terms",
        contact=Contact(name="Team", email="team@example.com"),
        license=License(name="MIT"),
        servers=[Server(url="https://api.example.com")],
        security=[{"BearerAuth": []}],
        external_docs=ExternalDocumentation(url="https://example.com/docs"),
        tags=[Tag(name="items", description="Item routes")],
    )
    api = BoltAPI(openapi_config=config)

    @api.get("/items", tags=["items"])
    async def list_items() -> list[Item]:
        return []

    api._register_openapi_routes()
    with TestClient(api) as client:
        document = client.get("/docs/openapi.json").json()

    assert document["info"] == {
        "title": "Test API",
        "version": "2.1.0",
        "description": "Long text.",
        "summary": "Short text.",
        "termsOfService": "https://example.com/terms",
        "contact": {"name": "Team", "email": "team@example.com"},
        "license": {"name": "MIT"},
    }
    assert document["servers"] == [{"url": "https://api.example.com"}]
    assert document["security"] == [{"BearerAuth": []}]
    assert document["externalDocs"] == {"url": "https://example.com/docs"}
    assert {"name": "items", "description": "Item routes"} in document["tags"]


def test_openapi_docs_ignore_the_prefix_of_the_api():
    """The docs routes keep their absolute path when the API has a prefix."""
    api = BoltAPI(prefix="/api", openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.get("/items")
    async def list_items() -> list[Item]:
        return []

    api._register_openapi_routes()
    with TestClient(api) as client:
        for path in ("/docs/openapi.json", "/docs/openapi.yaml", "/docs", "/docs/redoc"):
            assert client.get(path).status_code == 200, path
        assert client.get("/api/docs/openapi.json").status_code == 404
        assert "/api/items" in client.get("/docs/openapi.json").json()["paths"]


@pytest.mark.django_db(transaction=True)
def test_openapi_session_auth_redirects_to_login():
    """An anonymous browser goes to the login page of the session backend."""
    api = _docs_api_with_login(
        auth=[SessionAuthentication(login_url="/admin/login/")],
        guards=[IsAuthenticated()],
        render_plugins=[SwaggerRenderPlugin()],
    )

    with TestClient(api) as client:
        for route in ("/docs/openapi.json", "/docs/openapi.yaml", "/docs/openapi.yml", "/docs", "/docs/swagger"):
            response = client.get(route, follow_redirects=False)
            assert response.status_code == 302, f"{route}: {response.status_code}"
            assert response.headers["location"].startswith("/admin/login/?next="), response.headers["location"]


@pytest.mark.django_db(transaction=True)
def test_openapi_session_auth_ignores_global_jwt_default():
    """Docs with a session backend work when the project default is JWT.

    Regression test for https://github.com/dj-bolt/django-bolt/issues/378.
    """
    User.objects.create_user(username="docs_member", password="pw-for-tests")
    User.objects.create_user(username="docs_staff", password="pw-for-tests", is_staff=True)
    with override_settings(
        BOLT_AUTHENTICATION_CLASSES=[JWTAuthentication(secret="test-secret")],
        BOLT_DEFAULT_PERMISSION_CLASSES=[IsAuthenticated()],
    ):
        api = _docs_api_with_login(
            auth=[SessionAuthentication(login_url="/admin/login/")],
            guards=[Requires("is_staff", True)],
        )
        with TestClient(api) as member, TestClient(api) as staff:
            assert member.get("/docs", follow_redirects=False).status_code == 302
            for client, username in ((member, "docs_member"), (staff, "docs_staff")):
                response = client.post(f"/login?username={username}", headers={"sec-fetch-site": "same-origin"})
                assert response.status_code == 200, response.text

            assert member.get("/docs").status_code == 403
            for route in ("/docs", "/docs/openapi.json"):
                response = staff.get(route, follow_redirects=False)
                assert response.status_code == 200, f"{route}: {response.status_code} {response.text}"


def test_openapi_security_requirements_for_authenticated_routes():
    """Test that OpenAPI schema includes security requirements for routes with auth.

    Regression test for: https://github.com/FarhanAliRaza/django-bolt/pull/77
    The authorize button in Swagger UI was not shown because security extraction
    was looking for 'auth' key instead of '_auth_backend_instances'.
    """
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Auth Test API", version="1.0.0"))

    # Public route - no auth
    @api.get("/public")
    async def public_endpoint():
        """Public endpoint without authentication."""
        return {"status": "public"}

    # JWT protected route
    @api.get("/jwt-protected", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
    async def jwt_protected_endpoint():
        """JWT protected endpoint."""
        return {"status": "protected"}

    # API key protected route
    @api.get("/api-key-protected", auth=[APIKeyAuthentication(api_keys={"test-key"})], guards=[IsAuthenticated()])
    async def api_key_protected_endpoint():
        """API key protected endpoint."""
        return {"status": "api-protected"}

    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")
        assert response.status_code == 200

        schema = response.json()

        # Public route should NOT have security requirements
        public_path = schema["paths"].get("/public", {})
        public_get = public_path.get("get", {})
        assert "security" not in public_get, "Public route should not have security requirements"

        # JWT protected route SHOULD have security requirements
        jwt_path = schema["paths"].get("/jwt-protected", {})
        jwt_get = jwt_path.get("get", {})
        assert "security" in jwt_get, "JWT protected route must have security requirements for Swagger authorize button"
        jwt_security = jwt_get["security"]
        assert any("BearerAuth" in sec for sec in jwt_security), (
            f"JWT route should have BearerAuth security, got: {jwt_security}"
        )

        # API key protected route SHOULD have security requirements
        api_key_path = schema["paths"].get("/api-key-protected", {})
        api_key_get = api_key_path.get("get", {})
        assert "security" in api_key_get, "API key protected route must have security requirements"
        api_key_security = api_key_get["security"]
        assert any("ApiKeyAuth" in sec for sec in api_key_security), (
            f"API key route should have ApiKeyAuth security, got: {api_key_security}"
        )

        # SecuritySchemes MUST be auto-registered in components for Swagger UI
        # authorize button to work (clicking padlock should open token modal)
        components = schema.get("components", {})
        security_schemes = components.get("securitySchemes", {})

        assert "BearerAuth" in security_schemes, (
            f"BearerAuth must be auto-registered in components.securitySchemes, got: {security_schemes}"
        )
        assert security_schemes["BearerAuth"]["type"] == "http"
        assert security_schemes["BearerAuth"]["scheme"] == "bearer"

        assert "ApiKeyAuth" in security_schemes, (
            f"ApiKeyAuth must be auto-registered in components.securitySchemes, got: {security_schemes}"
        )
        assert security_schemes["ApiKeyAuth"]["type"] == "apiKey"
        assert security_schemes["ApiKeyAuth"]["name"] == "x-api-key"


def test_openapi_security_schemes_preserve_user_defined():
    """Test that user-defined security schemes are preserved and not overwritten."""
    custom_scheme = SecurityScheme(
        type="http",
        scheme="bearer",
        bearer_format="CustomJWT",
        description="My custom JWT scheme",
    )

    api = BoltAPI(
        openapi_config=OpenAPIConfig(
            title="Custom Auth API",
            version="1.0.0",
            components=Components(
                security_schemes={"BearerAuth": custom_scheme},
            ),
        )
    )

    @api.get("/protected", auth=[JWTAuthentication(secret="test")], guards=[IsAuthenticated()])
    async def protected():
        return {"ok": True}

    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")
        schema = response.json()
        schemes = schema["components"]["securitySchemes"]

        # User-defined scheme should be preserved, not overwritten
        assert schemes["BearerAuth"]["bearerFormat"] == "CustomJWT"
        assert schemes["BearerAuth"]["description"] == "My custom JWT scheme"


# The body of a 401 or a 403 of the Rust core, with no error formatter.
ERROR_DETAIL_SCHEMA = {
    "type": "object",
    "properties": {"detail": {"type": "string", "description": "Error message"}},
    "required": ["detail"],
}
WWW_AUTHENTICATE_HEADER = {
    "description": "The authentication challenge of the route",
    "schema": {"type": "string"},
    "required": False,
    "deprecated": False,
}


def _auth_errors_api(**api_options) -> BoltAPI:
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Auth Test API", version="1.0.0"), **api_options)
    api_key = APIKeyAuthentication(api_keys={"test-key"})

    @api.get("/public")
    async def public() -> dict:
        return {}

    @api.get("/user", auth=[api_key], guards=[IsAuthenticated()])
    async def user_only() -> dict:
        return {}

    @api.get("/manage", auth=[api_key], guards=[Requires("role", "admin")])
    async def admin_only() -> dict:
        return {}

    @api.get("/open", auth=[api_key], guards=[AllowAny()])
    async def allow_any() -> dict:
        return {}

    # AllowAny lets each request in: the guard after it does not run.
    @api.get("/open-first", auth=[api_key], guards=[AllowAny(), Requires("role", "admin")])
    async def allow_any_first() -> dict:
        return {}

    @api.get("/no-guard", auth=[api_key])
    async def no_guard() -> dict:
        return {}

    api._register_openapi_routes()
    return api


def _document(client: TestClient) -> dict:
    response = client.get("/docs/openapi.json")
    assert response.status_code == 200, response.text
    document = response.json()
    validate(document)
    return document


def test_openapi_documents_the_401_and_403_that_a_guarded_route_sends():
    """A route with an auth backend and a guard documents the error responses that it sends."""
    with TestClient(_auth_errors_api()) as client:
        paths = _document(client)["paths"]
        no_key = client.get("/user")
        no_key_admin = client.get("/manage")
        not_admin = client.get("/manage", headers={"x-api-key": "test-key"})
        statuses = {path: client.get(path).status_code for path in ("/public", "/open", "/open-first", "/no-guard")}

    responses = {path: paths[path]["get"]["responses"] for path in paths}
    assert sorted(responses["/user"]) == ["200", "401"]
    assert sorted(responses["/manage"]) == ["200", "401", "403"]
    # These routes cannot send a 401 or a 403.
    assert statuses == {"/public": 200, "/open": 200, "/open-first": 200, "/no-guard": 200}
    for path in statuses:
        assert sorted(responses[path]) == ["200"]

    assert responses["/user"]["401"] == {
        "description": "Unauthorized - The request has no valid credential",
        "headers": {"WWW-Authenticate": WWW_AUTHENTICATE_HEADER},
        "content": {"application/json": {"schema": ERROR_DETAIL_SCHEMA}},
    }
    assert responses["/manage"]["401"] == responses["/user"]["401"]
    assert responses["/manage"]["403"] == {
        "description": "Forbidden - A guard of the route denies the user",
        "content": {"application/json": {"schema": ERROR_DETAIL_SCHEMA}},
    }

    # The document shows what the client gets.
    for response in (no_key, no_key_admin):
        assert response.status_code == 401
        # The challenge names the scheme of the API key backend of the route.
        assert response.headers["www-authenticate"] == "ApiKey"
        assert response.json() == {"detail": "Authentication required"}
    assert not_admin.status_code == 403
    assert not_admin.json() == {"detail": "Permission denied"}
    for response in (no_key, not_admin):
        Draft202012Validator(ERROR_DETAIL_SCHEMA).validate(response.json())


@pytest.mark.parametrize(
    ("backends", "challenge"),
    [
        ([JWTAuthentication(secret="openapi-challenge-test-secret-32-b")], "Bearer"),
        ([JWTAuthentication(secret="openapi-challenge-test-secret-32-b", cookie="token")], "ApiKey"),
        ([JWTAuthentication(secret="openapi-challenge-test-secret-32-b", header="X-Token")], "ApiKey"),
        ([APIKeyAuthentication(api_keys={"test-key"})], "ApiKey"),
        ([SessionAuthentication()], "ApiKey"),
        (
            [JWTAuthentication(secret="openapi-challenge-test-secret-32-b"), APIKeyAuthentication(api_keys={"k"})],
            "Bearer, ApiKey",
        ),
    ],
    ids=["jwt", "jwt-cookie", "jwt-header", "api-key", "session", "jwt-and-api-key"],
)
def test_openapi_401_header_agrees_with_the_challenge_of_each_backend(backends, challenge):
    """The document promises a ``WWW-Authenticate`` header and names no scheme. The 401 has the scheme of the route."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Auth Test API", version="1.0.0"))

    @api.get("/user", auth=backends, guards=[IsAuthenticated()])
    async def user_only() -> dict:
        return {}

    api._register_openapi_routes()
    with TestClient(api) as client:
        document = _document(client)
        rejected = client.get("/user")

    operation = document["paths"]["/user"]["get"]
    documented = operation["responses"]["401"]
    # The challenge names "Bearer" for an "http" scheme of the document, and "ApiKey" for an "apiKey" scheme.
    schemes = document["components"]["securitySchemes"]
    scheme_types = [schemes[name]["type"] for requirement in operation["security"] for name in requirement]
    assert ", ".join(dict.fromkeys({"http": "Bearer", "apiKey": "ApiKey"}[kind] for kind in scheme_types)) == challenge

    assert documented["headers"] == {"WWW-Authenticate": WWW_AUTHENTICATE_HEADER}
    # The text is true for each backend: it names no scheme.
    assert documented["headers"]["WWW-Authenticate"]["description"] == "The authentication challenge of the route"
    assert "Bearer" not in documented["description"]
    assert rejected.status_code == 401
    assert rejected.headers["www-authenticate"] == challenge


def test_openapi_auth_errors_show_the_body_of_the_error_formatter():
    problem = {"application/problem+json": {"schema": {"$ref": "#/components/schemas/ProblemDetails"}}}

    with TestClient(_auth_errors_api(error_formatter=problem_details)) as client:
        responses = _document(client)["paths"]["/manage"]["get"]["responses"]
        no_key = client.get("/manage")
        not_admin = client.get("/manage", headers={"x-api-key": "test-key"})

    assert responses["401"]["content"] == problem
    assert responses["401"]["headers"] == {"WWW-Authenticate": WWW_AUTHENTICATE_HEADER}
    assert responses["403"]["content"] == problem
    assert (no_key.status_code, not_admin.status_code) == (401, 403)
    assert no_key.headers["www-authenticate"] == "ApiKey"
    for response in (no_key, not_admin):
        assert response.headers["content-type"] == "application/problem+json"
        msgspec.convert(response.json(), ProblemDetails)


def test_openapi_auth_errors_follow_include_error_responses_and_a_declared_response():
    class Locked(msgspec.Struct):
        reason: str

    api_key = APIKeyAuthentication(api_keys={"test-key"})
    hidden = BoltAPI(
        openapi_config=OpenAPIConfig(title="Auth Test API", version="1.0.0", include_error_responses=False)
    )
    declared = BoltAPI(openapi_config=OpenAPIConfig(title="Auth Test API", version="1.0.0"))

    @hidden.get("/manage", auth=[api_key], guards=[Requires("role", "admin")])
    async def hidden_admin() -> dict:
        return {}

    @declared.get(
        "/manage", auth=[api_key], guards=[Requires("role", "admin")], response_model={200: dict, 403: Locked}
    )
    async def declared_admin():
        return {}

    hidden._register_openapi_routes()
    declared._register_openapi_routes()
    with TestClient(hidden) as client:
        assert sorted(_document(client)["paths"]["/manage"]["get"]["responses"]) == ["200"]
    with TestClient(declared) as client:
        responses = _document(client)["paths"]["/manage"]["get"]["responses"]

    # The route can send its own 403 body and the 403 of the guard.
    assert responses["403"]["content"] == {
        "application/json": {"schema": {"anyOf": [{"$ref": "#/components/schemas/Locked"}, ERROR_DETAIL_SCHEMA]}}
    }
    assert "401" in responses


@pytest.mark.django_db
def test_openapi_session_route_with_login_url_documents_the_redirect():
    """A session backend with ``login_url`` redirects a request with no user. It does not send a 401."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Auth Test API", version="1.0.0"))

    @api.get("/page", auth=[SessionAuthentication(login_url="/accounts/login/")], guards=[Requires("is_staff")])
    async def page() -> dict:
        return {}

    @api.get("/data", auth=[SessionAuthentication()], guards=[IsAuthenticated()])
    async def data() -> dict:
        return {}

    api._register_openapi_routes()
    with TestClient(api) as client:
        paths = _document(client)["paths"]
        redirect = client.get("/page", follow_redirects=False)
        rejected = client.get("/data")

    assert sorted(paths["/page"]["get"]["responses"]) == ["200", "302", "403"]
    assert paths["/page"]["get"]["responses"]["302"] == {
        "description": "Found - Redirect to the login page",
        "headers": {"Location": {"schema": {"type": "string"}, "required": False, "deprecated": False}},
    }
    assert (redirect.status_code, redirect.headers["location"]) == (302, "/accounts/login/?next=/page")
    assert sorted(paths["/data"]["get"]["responses"]) == ["200", "401"]
    assert rejected.status_code == 401


def test_openapi_security_scheme_follows_the_place_of_the_credential():
    """Each backend gets the scheme of the place where Bolt reads its credential."""
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Auth Test API", version="1.0.0"))
    bearer = JWTAuthentication(secret="test-secret")
    jwt_cookie = JWTAuthentication(secret="test-secret", cookie="access_token")
    jwt_header = JWTAuthentication(secret="test-secret", header="X-Token")
    api_key = APIKeyAuthentication(api_keys={"test-key"})
    partner_key = APIKeyAuthentication(api_keys={"partner-key"}, header="X-Partner-Key")

    @api.get("/public")
    async def public() -> dict:
        return {}

    @api.get("/bearer", auth=[bearer], guards=[IsAuthenticated()])
    async def by_bearer() -> dict:
        return {}

    @api.get("/cookie", auth=[jwt_cookie], guards=[IsAuthenticated()])
    async def by_cookie() -> dict:
        return {}

    @api.get("/header", auth=[jwt_header], guards=[IsAuthenticated()])
    async def by_header() -> dict:
        return {}

    @api.get("/key", auth=[api_key], guards=[IsAuthenticated()])
    async def by_key() -> dict:
        return {}

    @api.get("/partner", auth=[partner_key], guards=[IsAuthenticated()])
    async def by_partner_key() -> dict:
        return {}

    @api.get("/each", auth=[bearer, api_key, SessionAuthentication()], guards=[IsAuthenticated()])
    async def by_each() -> dict:
        return {}

    api._register_openapi_routes()
    token = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, "test-secret", algorithm="HS256")
    with TestClient(api) as client:
        document = _document(client)
        # The requests that the schemes describe get a 200.
        assert client.get("/bearer", headers={"authorization": f"Bearer {token}"}).status_code == 200
        assert client.get("/cookie", headers={"cookie": f"access_token={token}"}).status_code == 200
        assert client.get("/cookie", headers={"authorization": f"Bearer {token}"}).status_code == 401
        assert client.get("/header", headers={"x-token": token}).status_code == 200
        assert client.get("/key", headers={"x-api-key": "test-key"}).status_code == 200
        assert client.get("/partner", headers={"x-partner-key": "partner-key"}).status_code == 200
        assert client.get("/partner", headers={"x-api-key": "partner-key"}).status_code == 401

    security = {path: item["get"].get("security") for path, item in document["paths"].items()}
    assert security == {
        "/public": None,
        "/bearer": [{"BearerAuth": []}],
        "/cookie": [{"JWTCookieAuth": []}],
        "/header": [{"JWTHeaderAuth": []}],
        "/key": [{"ApiKeyAuth": []}],
        "/partner": [{"ApiKeyAuth2": []}],
        # One entry for each backend: one of them is sufficient.
        "/each": [{"BearerAuth": []}, {"ApiKeyAuth": []}, {"SessionAuth": []}],
    }
    assert document["components"]["securitySchemes"] == {
        "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        "JWTCookieAuth": {"type": "apiKey", "in": "cookie", "name": "access_token", "description": "A JWT"},
        "JWTHeaderAuth": {"type": "apiKey", "in": "header", "name": "X-Token", "description": "A JWT"},
        "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "x-api-key"},
        "ApiKeyAuth2": {"type": "apiKey", "in": "header", "name": "X-Partner-Key"},
        "SessionAuth": {"type": "apiKey", "in": "cookie", "name": "sessionid"},
    }


def test_openapi_form_serializer_flattens_struct_fields():
    """Regression: when a Form() parameter is annotated with a Struct/Serializer,
    the runtime extractor reads each struct field as a top-level form key, so the
    multipart schema must mirror that — *not* nest the struct under the param name.
    Also covers UploadFile / list[UploadFile] schema shape inside and outside the
    struct.
    """

    class RegisterForm(Serializer):
        username: str
        age: int
        email: str
        attachments: list[UploadFile]

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.post("/register")
    async def register(
        payload: Annotated[RegisterForm, Form()],
        profile_picture: Annotated[UploadFile, File()],
    ):
        return {"ok": True}

    api._register_openapi_routes()

    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")
        assert response.status_code == 200, response.text

        schema = response.json()
        media = schema["paths"]["/register"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
        props = media["properties"]

        assert "payload" not in props, f"Struct should be flattened, got {list(props)}"
        assert {"username", "age", "email", "attachments", "profile_picture"} <= set(props)

        assert props["username"]["type"] == "string"
        assert props["age"]["type"] == "integer"
        assert props["profile_picture"] == {"type": "string", "format": "binary"}
        assert props["attachments"] == {"type": "array", "items": {"type": "string", "format": "binary"}}

        assert set(media["required"]) == {"username", "age", "email", "attachments", "profile_picture"}


def test_openapi_request_body_lists_the_content_types_that_the_route_accepts():
    """A route with a file accepts multipart only. A route with only form fields accepts both form types."""

    class Report(Serializer):
        title: str
        pages: list[UploadFile]

    api = BoltAPI(openapi_config=OpenAPIConfig(title="Test API", version="1.0.0"))

    @api.post("/avatar")
    async def avatar(file: Annotated[UploadFile, File()], title: Annotated[str, Form()]):
        return {"title": title}

    @api.post("/report")
    async def report(payload: Annotated[Report, Form()]):
        return {"title": payload.title}

    @api.post("/login")
    async def login(username: Annotated[str, Form()]):
        return {"username": username}

    api._register_openapi_routes()

    with TestClient(api) as client:
        paths = client.get("/docs/openapi.json").json()["paths"]
        content = {path: list(paths[path]["post"]["requestBody"]["content"]) for path in paths}

        assert content == {
            "/avatar": ["multipart/form-data"],
            "/report": ["multipart/form-data"],
            "/login": ["multipart/form-data", "application/x-www-form-urlencoded"],
        }
        # The document shows what the server does.
        assert client.post("/avatar", data={"title": "a", "file": "text"}).status_code == 422
        assert client.post("/avatar", data={"title": "a"}, files={"file": ("a.txt", b"x")}).json() == {"title": "a"}
        assert client.post("/login", data={"username": "ann"}).json() == {"username": "ann"}
        assert client.post("/login", data={"username": "ann"}, files={"x": ("a.txt", b"x")}).json() == {
            "username": "ann"
        }
