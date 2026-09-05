from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from .. import _json

if TYPE_CHECKING:
    pass

# Import yaml at module level with try/except for optional dependency
try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

__all__ = (
    "OpenAPIRenderPlugin",
    "JsonRenderPlugin",
    "YamlRenderPlugin",
    "SwaggerRenderPlugin",
    "RedocRenderPlugin",
    "ScalarRenderPlugin",
    "RapidocRenderPlugin",
    "StoplightRenderPlugin",
)

_favicon_url = "https://cdn.jsdelivr.net/gh/FarhanAliRaza/django-bolt@master/docs/favicon.png"
_default_favicon = f"<link rel='icon' type='image/png' href='{_favicon_url}'>"
_default_style = "<style>body { margin: 0; padding: 0 }</style>"


class OpenAPIRenderPlugin(ABC):
    """Base class for OpenAPI UI render plugins."""

    def __init__(
        self,
        *,
        path: str | list[str],
        media_type: str = "text/html; charset=utf-8",
        favicon: str = _default_favicon,
        style: str = _default_style,
    ) -> None:
        """Initialize the OpenAPI UI render plugin.

        Args:
            path: Path(s) to serve the UI at (relative to openapi_config.path).
            media_type: Media type for the response.
            favicon: HTML <link> tag for the favicon.
            style: Base styling of the html body.
        """
        self.paths = [path] if isinstance(path, str) else list(path)
        self.media_type = media_type
        self.favicon = favicon
        self.style = style

    @staticmethod
    def render_json(openapi_schema: dict[str, Any]) -> str:
        """Render the OpenAPI schema as JSON string.

        Args:
            openapi_schema: The OpenAPI schema as a dictionary.

        Returns:
            The rendered JSON as string.
        """
        return _json.encode(openapi_schema).decode("utf-8")

    @abstractmethod
    def render(self, openapi_schema: dict[str, Any], schema_url: str) -> str:
        """Render the OpenAPI UI.

        Args:
            openapi_schema: The OpenAPI schema as a dictionary.
            schema_url: URL to the OpenAPI JSON schema.

        Returns:
            The rendered HTML or data as string.
        """
        raise NotImplementedError

    def has_path(self, path: str) -> bool:
        """Check if the plugin serves a specific path.

        Args:
            path: The path to check.

        Returns:
            True if the plugin has the path, False otherwise.
        """
        return path in self.paths


class JsonRenderPlugin(OpenAPIRenderPlugin):
    """Render the OpenAPI schema as JSON."""

    def __init__(
        self,
        *,
        path: str | list[str] = "/openapi.json",
        media_type: str = "application/vnd.oai.openapi+json",
        **kwargs: Any,
    ) -> None:
        super().__init__(path=path, media_type=media_type, **kwargs)

    def render(self, openapi_schema: dict[str, Any], schema_url: str) -> dict[str, Any]:
        """Render OpenAPI schema as dict.

        Returns the schema dict directly so django-bolt's serialization
        layer can handle it with proper JSON encoding.
        """
        return openapi_schema


class YamlRenderPlugin(OpenAPIRenderPlugin):
    """Render the OpenAPI schema as YAML."""

    def __init__(
        self,
        *,
        path: str | list[str] = None,
        media_type: str = "text/yaml; charset=utf-8",
        **kwargs: Any,
    ) -> None:
        if path is None:
            path = ["/openapi.yaml", "/openapi.yml"]
        super().__init__(path=path, media_type=media_type, **kwargs)

    def render(self, openapi_schema: dict[str, Any], schema_url: str) -> str:
        """Render OpenAPI schema as YAML."""
        if yaml is not None:
            return yaml.dump(openapi_schema, default_flow_style=False)
        else:
            # Fallback to JSON if PyYAML not installed
            return "# PyYAML not installed. Install with: pip install pyyaml\n" + self.render_json(openapi_schema)


class SwaggerRenderPlugin(OpenAPIRenderPlugin):
    """Render the OpenAPI schema using Swagger UI."""

    def __init__(
        self,
        *,
        # Swagger UI versions before 5.32.0 reject OpenAPI 3.2 documents.
        # Bolt emits 3.2.0 when an API contains the QUERY HTTP method.
        version: str = "5.32.9",
        js_url: str | None = None,
        css_url: str | None = None,
        standalone_preset_js_url: str | None = None,
        path: str | list[str] = "/swagger",
        **kwargs: Any,
    ) -> None:
        """Initialize Swagger UI plugin.

        Args:
            version: Swagger UI version to download from CDN.
            js_url: Custom JS bundle URL (overrides version).
            css_url: Custom CSS bundle URL (overrides version).
            standalone_preset_js_url: Custom preset JS URL (overrides version).
            path: Path(s) to serve Swagger UI at.
            **kwargs: Additional arguments to pass to base class.
        """
        self.js_url = js_url or f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{version}/swagger-ui-bundle.js"
        self.css_url = css_url or f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{version}/swagger-ui.css"
        self.standalone_preset_js_url = (
            standalone_preset_js_url
            or f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{version}/swagger-ui-standalone-preset.js"
        )
        super().__init__(path=path, **kwargs)

    def render(self, openapi_schema: dict[str, Any], schema_url: str) -> str:
        """Render Swagger UI HTML page."""
        head = f"""
          <head>
            <title>{openapi_schema["info"]["title"]}</title>
            {self.favicon}
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <link href="{self.css_url}" rel="stylesheet">
            <script src="{self.js_url}" crossorigin></script>
            <script src="{self.standalone_preset_js_url}" crossorigin></script>
            {self.style}
          </head>
        """

        body = f"""
            <body>
              <div id='swagger-container'/>
                <script type='text/javascript'>
                const ui = SwaggerUIBundle({{
                  spec: {self.render_json(openapi_schema)},
                  dom_id: '#swagger-container',
                  deepLinking: true,
                  showExtensions: true,
                  showCommonExtensions: true,
                  presets: [
                      SwaggerUIBundle.presets.apis,
                      SwaggerUIBundle.SwaggerUIStandalonePreset
                  ],
                }})
                ui.initOAuth({{}})
            </script>
          </body>
        """

        return f"<!DOCTYPE html><html>{head}{body}</html>"


class RedocRenderPlugin(OpenAPIRenderPlugin):
    """Render the OpenAPI schema using Redoc."""

    def __init__(
        self,
        *,
        version: str = "2.5.3",
        js_url: str | None = None,
        google_fonts: bool = True,
        path: str | list[str] = "/redoc",
        **kwargs: Any,
    ) -> None:
        """Initialize Redoc plugin.

        Args:
            version: Redoc version to download from CDN.
            js_url: Custom JS bundle URL (overrides version).
            google_fonts: Download Google fonts via CDN.
            path: Path(s) to serve Redoc at.
            **kwargs: Additional arguments to pass to base class.
        """
        self.js_url = js_url or f"https://cdn.jsdelivr.net/npm/redoc@{version}/bundles/redoc.standalone.js"
        self.google_fonts = google_fonts
        super().__init__(path=path, **kwargs)

    def render(self, openapi_schema: dict[str, Any], schema_url: str) -> str:
        """Render Redoc HTML page."""
        head = f"""
          <head>
            <title>{openapi_schema["info"]["title"]}</title>
            {self.favicon}
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            """

        if self.google_fonts:
            head += """
            <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">
            """

        head += f"""
            <script src="{self.js_url}" crossorigin></script>
            {self.style}
          </head>
        """

        body = f"<body><div id='redoc-container'/><script type='text/javascript'>Redoc.init({self.render_json(openapi_schema)},undefined,document.getElementById('redoc-container'))</script></body>"

        return f"<!DOCTYPE html><html>{head}{body}</html>"


class ScalarRenderPlugin(OpenAPIRenderPlugin):
    """Render the OpenAPI schema using Scalar."""

    _default_css_url = "https://cdn.jsdelivr.net/gh/litestar-org/branding@main/assets/openapi/scalar.css"

    def __init__(
        self,
        *,
        version: str = "latest",
        js_url: str | None = None,
        css_url: str | None = None,
        path: str | list[str] = None,
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize Scalar plugin.

        Args:
            version: Scalar version to download from CDN.
            js_url: Custom JS bundle URL (overrides version).
            css_url: Custom CSS bundle URL (uses Litestar branding by default).
            path: Path(s) to serve Scalar at.
            options: Scalar configuration options.
            **kwargs: Additional arguments to pass to base class.
        """
        if path is None:
            path = ["/scalar", "/"]
        self.js_url = js_url or f"https://cdn.jsdelivr.net/npm/@scalar/api-reference@{version}"
        self.css_url = css_url or self._default_css_url
        self.options = options
        super().__init__(path=path, **kwargs)

    def render(self, openapi_schema: dict[str, Any], schema_url: str) -> str:
        """Render Scalar HTML page."""
        head = f"""
                  <head>
                    <title>{openapi_schema["info"]["title"]}</title>
                    {self.style}
                    <meta charset="utf-8"/>
                    <meta name="viewport" content="width=device-width, initial-scale=1">
                    {self.favicon}
                    <link rel="stylesheet" type="text/css" href="{self.css_url}">
                  </head>
                """

        options_script = ""
        if self.options:
            options_script = f"""
                <script>
                  document.getElementById('api-reference').dataset.configuration = '{__import__("django_bolt")._json.encode(self.options).decode()}'
                </script>
                """

        body = f"""
                <noscript>
                    Scalar requires Javascript to function. Please enable it to browse the documentation.
                </noscript>
                <script
                  id="api-reference"
                  data-url="{schema_url}">
                </script>
                {options_script}
                <script src="{self.js_url}" crossorigin></script>
                """

        return f"""
                <!DOCTYPE html>
                    <html>
                        {head}
                        {body}
                    </html>
                """


class RapidocRenderPlugin(OpenAPIRenderPlugin):
    """Render the OpenAPI schema using Rapidoc."""

    def __init__(
        self,
        *,
        version: str = "9.3.4",
        js_url: str | None = None,
        path: str | list[str] = "/rapidoc",
        **kwargs: Any,
    ) -> None:
        """Initialize Rapidoc plugin.

        Args:
            version: Rapidoc version to download from CDN.
            js_url: Custom JS bundle URL (overrides version).
            path: Path(s) to serve Rapidoc at.
            **kwargs: Additional arguments to pass to base class.
        """
        self.js_url = js_url or f"https://unpkg.com/rapidoc@{version}/dist/rapidoc-min.js"
        super().__init__(path=path, **kwargs)

    def render(self, openapi_schema: dict[str, Any], schema_url: str) -> str:
        """Render Rapidoc HTML page."""
        head = f"""
          <head>
            <title>{openapi_schema["info"]["title"]}</title>
            {self.favicon}
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <script src="{self.js_url}" crossorigin></script>
            {self.style}
          </head>
        """

        body = f"""
          <body>
            <rapi-doc spec-url="{schema_url}" />
          </body>
        """

        return f"""
        <!DOCTYPE html>
            <html>
                {head}
                {body}
            </html>
        """


class StoplightRenderPlugin(OpenAPIRenderPlugin):
    """Render an OpenAPI schema using StopLight Elements."""

    def __init__(
        self,
        *,
        version: str = "7.7.18",
        js_url: str | None = None,
        css_url: str | None = None,
        path: str | list[str] = "/elements",
        **kwargs: Any,
    ) -> None:
        """Initialize the OpenAPI UI render plugin.

        Args:
            version: StopLight Elements version to download from the CDN. If js_url is provided, this is ignored.
            js_url: Download url for the StopLight Elements JS bundle. If not provided, the version will be used to
                construct the url.
            css_url: Download url for the StopLight Elements CSS bundle. If not provided, the version will be used to
                construct the url.
            path: Path to serve the OpenAPI UI at.
            **kwargs: Additional arguments to pass to the base class.
        """
        self.js_url = js_url or f"https://unpkg.com/@stoplight/elements@{version}/web-components.min.js"
        self.css_url = css_url or f"https://unpkg.com/@stoplight/elements@{version}/styles.min.css"
        super().__init__(path=path, **kwargs)

    def render(self, openapi_schema: dict[str, Any], schema_url: str) -> str:
        """Render an HTML page for StopLight Elements.

        Args:
            openapi_schema: The OpenAPI schema as a dictionary.
            schema_url: URL to the OpenAPI JSON schema.

        Returns:
            A rendered HTML string.
        """
        head = f"""
          <head>
            <title>{openapi_schema["info"]["title"]}</title>
            {self.favicon}
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
            <link rel="stylesheet" href="{self.css_url}">
            <script src="{self.js_url}" crossorigin></script>
            {self.style}
          </head>
        """

        body = f"""
          <body>
            <elements-api
                apiDescriptionUrl="{schema_url}"
                router="hash"
                layout="sidebar"
            />
          </body>
        """

        return f"""
        <!DOCTYPE html>
            <html>
                {head}
                {body}
            </html>
        """
