"""
Static file route registration for Django admin.

This module handles the registration of static file serving routes
needed by Django admin and other Django apps.
"""

import inspect
import sys
from typing import TYPE_CHECKING

from django.conf import settings

from django_bolt.admin.static import serve_static_file
from django_bolt.middleware.compiler import (
    add_optimization_flags_to_metadata,
    compile_middleware_meta,
)
from django_bolt.serialization import compile_response_handlers
from django_bolt.typing import FieldDefinition

if TYPE_CHECKING:
    from django_bolt.api import BoltAPI


class StaticRouteRegistrar:
    """Handles registration of static file serving routes."""

    def __init__(self, api: "BoltAPI"):
        """Initialize the registrar with a BoltAPI instance.

        Args:
            api: The BoltAPI instance to register routes on
        """
        self.api = api

    def register_routes(self) -> None:
        """Register static file serving routes for Django admin.

        This enables serving of CSS, JS, and other static assets needed
        by Django admin and other Django apps.
        """
        if self.api._static_routes_registered:
            return

        # Only register static routes if admin routes were registered
        # (admin needs static files for CSS/JS)
        if not self.api._admin_routes_registered:
            return

        try:
            # Check if static files are configured
            if not hasattr(settings, "STATIC_URL") or not settings.STATIC_URL:
                return

            static_url = settings.STATIC_URL.strip("/")
            if not static_url:
                static_url = "static"

            # Register catch-all route for static files
            route_pattern = f"/{static_url}/{{path:path}}"

            # Create static file handler
            async def static_handler(path: str):
                """Serve static files for Django admin and other apps."""
                return await serve_static_file(path)

            # Register the route
            self._register_static_route(route_pattern, static_handler)

            self.api._static_routes_registered = True

        except Exception as e:
            print(f"[django-bolt] Warning: Failed to register static routes: {e}", file=sys.stderr)

    def _register_static_route(self, route_pattern: str, handler) -> None:
        """Register a static file serving route.

        Args:
            route_pattern: URL path pattern (e.g., /static/{path:path})
            handler: Async handler function for serving static files
        """
        handler_id = self.api._next_handler_id
        self.api._next_handler_id += 1

        self.api._routes.append(("GET", route_pattern, handler_id, handler))
        self.api._handlers[handler_id] = handler

        # Create metadata for static handler
        # Extract path parameter metadata using FieldDefinition
        sig = inspect.signature(handler)
        path_field = FieldDefinition(
            name="path",
            annotation=str,
            default=inspect.Parameter.empty,
            source="path",
            alias=None,
            embed=False,
            dependency=None,
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )

        meta = {
            "mode": "mixed",
            "sig": sig,
            "fields": [path_field],
            "path_params": {"path"},
            "is_async": True,  # static_handler is async
        }

        # Create injector for static handler (extracts path param from request)
        def static_injector(request):
            path_value = request["params"].get("path", "")
            return ([path_value], {})

        meta["injector"] = static_injector
        meta["injector_is_async"] = False

        # Guarantee all required metadata keys exist (same as _route_decorator).
        # Without these, _dispatch will KeyError on the hot path.
        meta["response_type"] = None
        meta["_stream_info"] = (False, None)
        meta["is_multi_response"] = False
        meta["default_status_code"] = 200
        meta["validate_response"] = False
        meta["_router_middleware"] = []
        meta["_route_middleware"] = []
        meta["_has_route_python_middleware"] = False
        meta["has_file_uploads"] = False

        self.api._handler_meta[handler_id] = meta

        # Compile response handlers (required by serialize_response).
        compile_response_handlers(meta)

        # Compile the handler executor (required by _dispatch for both fast
        # path and middleware path).  Must happen after meta is stored because
        # _compile_handler_executor reads from the meta dict.
        meta["_handler_executor"] = self.api._compile_handler_executor(meta)

        # Compile middleware metadata so Rust-side optimization flags are set.
        middleware_meta = compile_middleware_meta(handler, "GET", route_pattern, list(self.api._middleware))
        middleware_meta = add_optimization_flags_to_metadata(middleware_meta, meta)
        if middleware_meta:
            self.api._handler_middleware[handler_id] = middleware_meta
