"""
OpenAPI route registration for BoltAPI.

This module handles the registration of OpenAPI documentation routes
(JSON, YAML, and UI plugins) separately from the main BoltAPI class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.decorators import login_required

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
        self._docs_api = None  # Separate API for docs when using django_auth

    def _get_django_auth_decorator(self):
        """Get the Django auth decorator if configured.

        Returns:
            The decorator function, or None if not configured.
        """
        django_auth = self.api._openapi_config.django_auth
        if django_auth is None:
            return None
        if django_auth is True:
            return login_required
        # It's a decorator function (e.g., staff_member_required)
        return django_auth

    def _apply_django_auth(self, handler):
        """Apply Django auth decorator to handler if configured.

        Args:
            handler: The async handler function

        Returns:
            The wrapped handler if django_auth is configured, otherwise original handler.
        """
        decorator = self._get_django_auth_decorator()
        if decorator:
            return decorator(handler)
        return handler

    def _get_docs_api(self):
        """Get or create the API instance for registering docs routes.

        If django_auth is configured, creates a separate BoltAPI with
        django_middleware=True and mounts it at the docs path.
        Otherwise, returns the main API for direct registration.
        """
        if self.api._openapi_config.django_auth:
            if self._docs_api is None:
                from django_bolt.api import BoltAPI  # noqa: PLC0415

                # Create separate API with Django middleware for docs
                self._docs_api = BoltAPI(
                    django_middleware=True,
                    enable_logging=False,
                )
            return self._docs_api
        return self.api

    def register_routes(self) -> None:
        """Register OpenAPI documentation routes.

        This registers:
        - /docs/openapi.json - JSON schema endpoint
        - /docs/openapi.yaml - YAML schema endpoint
        - /docs/openapi.yml - YAML schema endpoint (alternative)
        - UI plugin routes (e.g., /docs/swagger, /docs/redoc)
        - Root redirect to default UI

        When django_auth is configured, routes are registered on a separate
        BoltAPI with django_middleware=True and mounted at the docs path.
        """
        if not self.api._openapi_config or self.api._openapi_routes_registered:
            return

        # Check if docs are enabled
        if not self.api._openapi_config.enabled:
            return

        # Get the API to register routes on (separate API if using django_auth)
        docs_api = self._get_docs_api()
        if docs_api is not self.api:
            # Authenticated docs already run the Django middleware stack.
            self._middleware = None
        use_django_auth = self.api._openapi_config.django_auth is not None

        # Get guards and auth from config for protecting doc routes
        guards = self.api._openapi_config.guards
        auth = self.api._openapi_config.auth

        # Determine the base path for routes
        # If using mounted docs API, routes are relative (no prefix)
        # If using main API, routes include the full path
        route_prefix = "" if use_django_auth else self.api._openapi_config.path

        # When registering on main API (not using django_auth), skip the API prefix
        # so docs are at absolute paths (e.g., /docs/*) regardless of API prefix
        skip_prefix = not use_django_auth

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

        openapi_json_handler = self._apply_django_auth(openapi_json_handler)
        docs_api._route_decorator(
            "GET",
            f"{route_prefix}/openapi.json",
            guards=guards,
            auth=auth,
            _skip_prefix=skip_prefix,
            _router_middleware=self._middleware,
        )(openapi_json_handler)

        # Always register YAML endpoints
        yaml_plugin = YamlRenderPlugin()

        async def openapi_yaml_handler(request):
            """Serve OpenAPI schema as YAML."""
            schema = self._get_schema()
            rendered = yaml_plugin.render(schema, "")
            return PlainText(rendered, status_code=200, headers={"content-type": yaml_plugin.media_type})

        openapi_yaml_handler = self._apply_django_auth(openapi_yaml_handler)
        docs_api._route_decorator(
            "GET",
            f"{route_prefix}/openapi.yaml",
            guards=guards,
            auth=auth,
            _skip_prefix=skip_prefix,
            _router_middleware=self._middleware,
        )(openapi_yaml_handler)

        async def openapi_yml_handler(request):
            """Serve OpenAPI schema as YAML (alternative extension)."""
            schema = self._get_schema()
            rendered = yaml_plugin.render(schema, "")
            return PlainText(rendered, status_code=200, headers={"content-type": yaml_plugin.media_type})

        openapi_yml_handler = self._apply_django_auth(openapi_yml_handler)
        docs_api._route_decorator(
            "GET",
            f"{route_prefix}/openapi.yml",
            guards=guards,
            auth=auth,
            _skip_prefix=skip_prefix,
            _router_middleware=self._middleware,
        )(openapi_yml_handler)

        # Register UI plugin routes
        self._register_ui_plugins(docs_api, route_prefix, skip_prefix)

        # Add root redirect to default plugin
        self._register_root_redirect(docs_api, route_prefix, skip_prefix)

        # If using separate docs API, mount it at the docs path
        if use_django_auth and self._docs_api is not None:
            self.api.mount(self.api._openapi_config.path, self._docs_api)

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

    def _register_ui_plugins(self, docs_api, route_prefix: str, skip_prefix: bool) -> None:
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
                handler = self._apply_django_auth(handler)
                docs_api._route_decorator(
                    "GET",
                    full_path,
                    guards=guards,
                    auth=auth,
                    _skip_prefix=skip_prefix,
                    _router_middleware=self._middleware,
                )(handler)

    def _register_root_redirect(self, docs_api, route_prefix: str, skip_prefix: bool) -> None:
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

            # For mounted API, register at empty string so mount adds just the prefix
            # e.g., "" mounted at "/docs" becomes "/docs" (not "/docs/")
            # For main API, use the full path
            use_django_auth = self.api._openapi_config.django_auth is not None
            root_path = "" if use_django_auth else route_prefix

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
            handler = self._apply_django_auth(handler)
            docs_api._route_decorator(
                "GET",
                root_path,
                guards=guards,
                auth=auth,
                _skip_prefix=skip_prefix,
                _router_middleware=self._middleware,
            )(handler)
