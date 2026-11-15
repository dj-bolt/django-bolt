"""
Router for Django-Bolt API.

Provides hierarchical routing with middleware inheritance.
Routes defined on a router inherit the router's middleware, auth, and guards.
A nested router inherits the middleware, auth, guards and tags of its parent.
"""

from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any, Optional

from .middleware.middleware import normalize_middleware_specs

if TYPE_CHECKING:
    from .auth.backends import AuthenticationBackend
    from .auth.guards import Guard
    from .middleware import MiddlewareType


def _leading_slash(path: str) -> str:
    """Give a path or a prefix its leading slash. An empty value stays empty."""
    return path if not path or path.startswith("/") else "/" + path


class Router:
    """
    Router for grouping routes with common configuration.

    Supports:
    - URL prefixes
    - Middleware inheritance (routes inherit router middleware)
    - Authentication/guard defaults
    - OpenAPI tags
    - Nested routers: a child router takes the auth, guards and tags that it
      does not set from the nearest parent router that sets them

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
            middleware: Router-level middleware (inherits from parent)
            auth: Default authentication backends (can be overridden per-route)
            guards: Default guards/permissions (can be overridden per-route)
            parent: Parent router for nested routers. The routes of this
                router get the prefix and the middleware of the parent.
                `api.include_router()` registers them one time, if it gets
                this router, the parent, or the two.
        """
        self.prefix = _leading_slash(prefix.rstrip("/"))
        self.tags = tags or []
        self.middleware = normalize_middleware_specs(middleware, context="router")
        self.auth = auth
        self.guards = guards
        self._routes: list[tuple[str, str, Callable, dict[str, Any]]] = []
        self._parent = parent
        self._children: list[Router] = []

        if parent:
            parent._children.append(self)

    def _add(self, method: str, path: str, handler: Callable, **meta: Any) -> None:
        """Add a route to the router.

        The path stays relative. `get_all_routes` adds the prefix, so a prefix
        that `include_router` changes later applies to each route.
        """
        # Merge router-level defaults into meta
        if self.tags and "tags" not in meta:
            meta["tags"] = self.tags
        if self.auth is not None and "auth" not in meta:
            meta["auth"] = self.auth
        if self.guards is not None and "guards" not in meta:
            meta["guards"] = self.guards
        meta["_owner_router"] = self
        self._routes.append((method, path, handler, meta))

    def get(self, path: str, **kwargs: Any):
        """Register a GET route."""

        def dec(fn: Callable):
            self._add("GET", _leading_slash(path), fn, **kwargs)
            return fn

        return dec

    def post(self, path: str, **kwargs: Any):
        """Register a POST route."""

        def dec(fn: Callable):
            self._add("POST", _leading_slash(path), fn, **kwargs)
            return fn

        return dec

    def put(self, path: str, **kwargs: Any):
        """Register a PUT route."""

        def dec(fn: Callable):
            self._add("PUT", _leading_slash(path), fn, **kwargs)
            return fn

        return dec

    def patch(self, path: str, **kwargs: Any):
        """Register a PATCH route."""

        def dec(fn: Callable):
            self._add("PATCH", _leading_slash(path), fn, **kwargs)
            return fn

        return dec

    def delete(self, path: str, **kwargs: Any):
        """Register a DELETE route."""

        def dec(fn: Callable):
            self._add("DELETE", _leading_slash(path), fn, **kwargs)
            return fn

        return dec

    def head(self, path: str, **kwargs: Any):
        """Register a HEAD route."""

        def dec(fn: Callable):
            self._add("HEAD", _leading_slash(path), fn, **kwargs)
            return fn

        return dec

    def options(self, path: str, **kwargs: Any):
        """Register an OPTIONS route."""

        def dec(fn: Callable):
            self._add("OPTIONS", _leading_slash(path), fn, **kwargs)
            return fn

        return dec

    def query(self, path: str, **kwargs: Any):
        """Register a QUERY route."""

        def dec(fn: Callable):
            self._add("QUERY", _leading_slash(path), fn, **kwargs)
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
        # A router made with `parent=self` is already a child. Do not add it again.
        if router not in self._children:
            self._children.append(router)

        # Combine prefixes
        if prefix:
            router.prefix = _leading_slash(prefix.rstrip("/")) + router.prefix

    def get_all_routes(self, skip: Collection["Router"] = ()) -> list[tuple[str, str, Callable, dict[str, Any]]]:
        """
        Get all routes including from child routers.

        Args:
            skip: Child routers to leave out, with their own children.

        Returns list of (method, path, handler, meta) tuples.
        """
        routes = []
        for method, path, handler, meta in self._routes:
            route_meta = dict(meta)
            owner_router = route_meta.pop("_owner_router", self)
            owner_router._inherit_parent_defaults(route_meta)
            route_meta["_router_middleware"] = owner_router.get_middleware_chain()
            routes.append((method, self.prefix + path, handler, route_meta))

        for child in self._children:
            if child in skip:
                continue
            # Prepend parent prefix to child routes
            for method, path, handler, meta in child.get_all_routes(skip):
                routes.append((method, self.prefix + path, handler, meta))
        return routes

    def parent_prefix(self) -> str:
        """The prefixes of the parent routers, from the root to the direct parent."""
        parent = self._parent
        return parent.parent_prefix() + parent.prefix if parent else ""

    def child_routers(self) -> list["Router"]:
        """Each router below this one: its children, and their children."""
        routers = []
        for child in self._children:
            routers.append(child)
            routers.extend(child.child_routers())
        return routers

    def _inherit_parent_defaults(self, meta: dict[str, Any]) -> None:
        """Fill auth, guards and tags that the route and this router do not set.

        The nearest parent router that sets a value gives it. The parent of a
        router can change until the API includes the routers, so this runs then.
        """
        parent = self._parent
        while parent is not None:
            if parent.tags and "tags" not in meta:
                meta["tags"] = parent.tags
            if parent.auth is not None and "auth" not in meta:
                meta["auth"] = parent.auth
            if parent.guards is not None and "guards" not in meta:
                meta["guards"] = parent.guards
            parent = parent._parent

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
