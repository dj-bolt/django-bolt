"""
OpenAPI route registration for BoltAPI.

This module handles the registration of OpenAPI documentation routes
(JSON, YAML, and UI plugins) separately from the main BoltAPI class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django_bolt.openapi.plugins import JsonRenderPlugin, YamlRenderPlugin
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.responses import HTML, JSON, PlainText

if TYPE_CHECKING:
    from django_bolt.api import BoltAPI


class OpenAPIRouteRegistrar:
    """Handles registration of OpenAPI documentation routes."""

    def __init__(self, api: BoltAPI, *, middleware: list[Any] | None = None):
        """Initialize the registrar with a BoltAPI instance.

        Args:
            api: The BoltAPI instance to register routes on
        """
        self.api = api
        self._middleware = middleware

    def register_routes(self) -> None:
        """Register OpenAPI documentation routes.

        This registers:
        - /docs/openapi.json - JSON schema endpoint
        - /docs/openapi.yaml - YAML schema endpoint
        - /docs/openapi.yml - YAML schema endpoint (alternative)
        - UI plugin routes (e.g., /docs/swagger, /docs/redoc)
        - Root redirect to default UI
        """
        if not self.api._openapi_config or self.api._openapi_routes_registered:
            return

        # Check if docs are enabled
        if not self.api._openapi_config.enabled:
            return

        # Get guards and auth from config for protecting doc routes
        guards = self.api._openapi_config.guards
        auth = self.api._openapi_config.auth

        # Docs are at absolute paths (e.g., /docs/*) regardless of API prefix,
        # so each docs route registers with _skip_prefix=True.
        route_prefix = self.api._openapi_config.path

        # Always register JSON endpoint
        json_plugin = JsonRenderPlugin()

        async def openapi_json_handler(request):
            """Serve OpenAPI schema as JSON."""
            try:
                schema = self._get_schema()
                rendered = json_plugin.render(schema, "")
                return JSON(rendered, status_code=200, headers={"content-type": json_plugin.media_type})
            except Exception as e:
                raise Exception(f"Failed to generate OpenAPI JSON schema: {type(e).__name__}: {str(e)}") from e

        self.api._route_decorator(
            "GET",
            f"{route_prefix}/openapi.json",
            guards=guards,
            auth=auth,
            _skip_prefix=True,
            _router_middleware=self._middleware,
        )(openapi_json_handler)

        # Always register YAML endpoints
        yaml_plugin = YamlRenderPlugin()

        async def openapi_yaml_handler(request):
            """Serve OpenAPI schema as YAML."""
            schema = self._get_schema()
            rendered = yaml_plugin.render(schema, "")
            return PlainText(rendered, status_code=200, headers={"content-type": yaml_plugin.media_type})

        self.api._route_decorator(
            "GET",
            f"{route_prefix}/openapi.yaml",
            guards=guards,
            auth=auth,
            _skip_prefix=True,
            _router_middleware=self._middleware,
        )(openapi_yaml_handler)

        async def openapi_yml_handler(request):
            """Serve OpenAPI schema as YAML (alternative extension)."""
            schema = self._get_schema()
            rendered = yaml_plugin.render(schema, "")
            return PlainText(rendered, status_code=200, headers={"content-type": yaml_plugin.media_type})

        self.api._route_decorator(
            "GET",
            f"{route_prefix}/openapi.yml",
            guards=guards,
            auth=auth,
            _skip_prefix=True,
            _router_middleware=self._middleware,
        )(openapi_yml_handler)

        # Register UI plugin routes
        self._register_ui_plugins(route_prefix)

        # Add root redirect to default plugin
        self._register_root_redirect(route_prefix)

        self.api._openapi_routes_registered = True

    def _get_schema(self) -> dict[str, Any]:
        """Get or generate OpenAPI schema.

        Returns:
            OpenAPI schema as dictionary
        """
        if self.api._openapi_schema is None:
            generator = SchemaGenerator(self.api, self.api._openapi_config)
            openapi = generator.generate()
            self.api._openapi_schema = openapi.to_schema()

        return self.api._openapi_schema

    def _register_ui_plugins(self, route_prefix: str) -> None:
        """Register UI plugin routes (Swagger UI, ReDoc, etc.)."""
        # Schema URL is always the full path (for the UI to fetch)
        schema_url = f"{self.api._openapi_config.path}/openapi.json"
        guards = self.api._openapi_config.guards
        auth = self.api._openapi_config.auth

        for plugin in self.api._openapi_config.render_plugins:
            for plugin_path in plugin.paths:
                full_path = f"{route_prefix}{plugin_path}"

                # Create closure to capture plugin reference
                def make_handler(p):
                    response_class = (
                        JSON
                        if isinstance(p, JsonRenderPlugin)
                        else PlainText
                        if isinstance(p, YamlRenderPlugin)
                        else HTML
                    )

                    async def ui_handler(request):
                        """Serve OpenAPI UI."""
                        try:
                            schema = self._get_schema()
                            rendered = p.render(schema, schema_url)
                            return response_class(rendered, status_code=200, headers={"content-type": p.media_type})
                        except Exception as e:
                            raise Exception(
                                f"Failed to render OpenAPI UI plugin {p.__class__.__name__}: "
                                f"{type(e).__name__}: {str(e)}"
                            ) from e

                    return ui_handler

                handler = make_handler(plugin)
                self.api._route_decorator(
                    "GET",
                    full_path,
                    guards=guards,
                    auth=auth,
                    _skip_prefix=True,
                    _router_middleware=self._middleware,
                )(handler)

    def _register_root_redirect(self, route_prefix: str) -> None:
        """Register root path to serve default UI directly.

        Serves the default UI at the root path instead of redirecting.
        This avoids redirect loops caused by NormalizePath::trim() middleware
        which strips trailing slashes (e.g., /docs/ -> /docs).
        """
        if self.api._openapi_config.default_plugin:
            # Schema URL is always the full path (for the UI to fetch)
            schema_url = f"{self.api._openapi_config.path}/openapi.json"
            plugin = self.api._openapi_config.default_plugin
            guards = self.api._openapi_config.guards
            auth = self.api._openapi_config.auth

            # Capture plugin in closure
            def make_root_handler(p, url):
                response_class = (
                    JSON if isinstance(p, JsonRenderPlugin) else PlainText if isinstance(p, YamlRenderPlugin) else HTML
                )

                async def openapi_root_handler(request):
                    """Serve default OpenAPI UI at root path."""
                    try:
                        schema = self._get_schema()
                        rendered = p.render(schema, url)
                        return response_class(rendered, status_code=200, headers={"content-type": p.media_type})
                    except Exception as e:
                        raise Exception(f"Failed to render OpenAPI UI: {type(e).__name__}: {str(e)}") from e

                return openapi_root_handler

            handler = make_root_handler(plugin, schema_url)
            self.api._route_decorator(
                "GET",
                route_prefix,
                guards=guards,
                auth=auth,
                _skip_prefix=True,
                _router_middleware=self._middleware,
            )(handler)
