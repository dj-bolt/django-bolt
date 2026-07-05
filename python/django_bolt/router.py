"""
Router for Django-Bolt API.

Provides hierarchical routing with middleware inheritance.
Routes defined on a router inherit the router's middleware, auth, and guards.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from .middleware.middleware import normalize_middleware_specs

if TYPE_CHECKING:
    from .auth.backends import AuthenticationBackend
    from .auth.guards import Guard
    from .middleware import MiddlewareType


class Router:
    """
    Router for grouping routes with common configuration.

    Supports:
    - URL prefixes
    - Middleware inheritance (routes inherit router middleware)
    - Authentication/guard defaults
    - OpenAPI tags

    Example:
        # Create router with middleware
        admin_router = Router(
            prefix="/admin",
            middleware=[AdminAuthMiddleware],
            tags=["admin"],
        )

        @admin_router.get("/users")
        async def list_users(request: Request) -> list:
            return await User.objects.all()

        # Include in main app
        api.include_router(admin_router)

    Middleware Inheritance:
        Routes on a router inherit middleware from:
        1. App-level middleware (BoltAPI.middleware)
        2. Router-level middleware (Router.middleware)
        3. Route-level middleware (@middleware decorator)

        Execution order: App -> Router -> Route -> Handler
    """

    __slots__ = (
        "prefix",
        "tags",
        "dependencies",
        "middleware",
        "auth",
        "guards",
        "_routes",
        "_parent",
        "_children",
    )

    def __init__(
        self,
        prefix: str = "",
        tags: list[str] | None = None,
        dependencies: list[Any] | None = None,
        middleware: list["MiddlewareType"] | None = None,
        auth: list["AuthenticationBackend"] | None = None,
        guards: list["Guard"] | None = None,
        parent: Optional["Router"] = None,
    ):
        """
        Initialize a router.

        Args:
            prefix: URL prefix for all routes (e.g., "/api/v1")
            tags: OpenAPI tags for documentation
            dependencies: Shared dependencies for all routes
            middleware: Router-level middleware (inherits from parent)
            auth: Default authentication backends (can be overridden per-route)
            guards: Default guards/permissions (can be overridden per-route)
            parent: Parent router for nested routers
        """
        self.prefix = prefix.rstrip("/")
        self.tags = tags or []
        self.dependencies = dependencies or []
        self.middleware = normalize_middleware_specs(middleware, context="router")
        self.auth = auth
        self.guards = guards
        self._routes: list[tuple[str, str, Callable, dict[str, Any]]] = []
        self._parent = parent
        self._children: list[Router] = []

        if parent:
            parent._children.append(self)

    def _add(self, method: str, path: str, handler: Callable, **meta: Any) -> None:
        """Add a route to the router."""
        full = (self.prefix + path) if self.prefix else path
        # Merge router-level defaults into meta
        if self.tags and "tags" not in meta:
            meta["tags"] = self.tags
        if self.auth is not None and "auth" not in meta:
            meta["auth"] = self.auth
        if self.guards is not None and "guards" not in meta:
            meta["guards"] = self.guards
        meta["_owner_router"] = self
        self._routes.append((method, full, handler, meta))

    def get(self, path: str, **kwargs: Any):
        """Register a GET route."""

        def dec(fn: Callable):
            self._add("GET", path, fn, **kwargs)
            return fn

        return dec

    def post(self, path: str, **kwargs: Any):
        """Register a POST route."""

        def dec(fn: Callable):
            self._add("POST", path, fn, **kwargs)
            return fn

        return dec

    def put(self, path: str, **kwargs: Any):
        """Register a PUT route."""

        def dec(fn: Callable):
            self._add("PUT", path, fn, **kwargs)
            return fn

        return dec

    def patch(self, path: str, **kwargs: Any):
        """Register a PATCH route."""

        def dec(fn: Callable):
            self._add("PATCH", path, fn, **kwargs)
            return fn

        return dec

    def delete(self, path: str, **kwargs: Any):
        """Register a DELETE route."""

        def dec(fn: Callable):
            self._add("DELETE", path, fn, **kwargs)
            return fn

        return dec

    def head(self, path: str, **kwargs: Any):
        """Register a HEAD route."""

        def dec(fn: Callable):
            self._add("HEAD", path, fn, **kwargs)
            return fn

        return dec

    def options(self, path: str, **kwargs: Any):
        """Register an OPTIONS route."""

        def dec(fn: Callable):
            self._add("OPTIONS", path, fn, **kwargs)
            return fn

        return dec

    def query(self, path: str, **kwargs: Any):
        """Register a QUERY route."""

        def dec(fn: Callable):
            self._add("QUERY", path, fn, **kwargs)
            return fn

        return dec

    def include_router(
        self,
        router: "Router",
        prefix: str = "",
    ) -> None:
        """
        Include another router as a child.

        Args:
            router: Router to include
            prefix: Additional prefix (combined with router's prefix)

        Example:
            v1_router = Router(prefix="/v1")
            users_router = Router(prefix="/users")

            @users_router.get("")
            async def list_users():
                return []

            v1_router.include_router(users_router)
            # Results in: GET /v1/users
        """
        # Update the child router's parent relationship
        router._parent = self
        self._children.append(router)

        # Combine prefixes
        if prefix:
            router.prefix = prefix.rstrip("/") + router.prefix

    def get_all_routes(self) -> list[tuple[str, str, Callable, dict[str, Any]]]:
        """
        Get all routes including from child routers.

        Returns list of (method, path, handler, meta) tuples.
        """
        routes = []
        for method, path, handler, meta in self._routes:
            route_meta = dict(meta)
            owner_router = route_meta.pop("_owner_router", self)
            route_meta["_router_middleware"] = owner_router.get_middleware_chain()
            routes.append((method, path, handler, route_meta))

        for child in self._children:
            # Prepend parent prefix to child routes
            for method, path, handler, meta in child.get_all_routes():
                full_path = self.prefix + path if self.prefix else path
                routes.append((method, full_path, handler, meta))
        return routes

    def get_middleware_chain(self) -> list["MiddlewareType"]:
        """
        Get the full middleware chain including parent middleware.

        Returns middleware in execution order (parent first).
        """
        chain = []
        if self._parent:
            chain.extend(self._parent.get_middleware_chain())
        chain.extend(self.middleware)
        return chain


__all__ = ["Router"]
