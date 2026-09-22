"""
Type definitions for Django-Bolt.

This module provides type hints and protocols for Django-Bolt objects,
enabling full IDE autocomplete and static type checking.

Includes:
- Request/AuthContext/UserType protocols (for Rust-backed objects)
- Type aliases for common authentication patterns (JWTClaims, APIKeyAuth, etc.)
- Request type aliases (AuthenticatedRequest, PublicRequest, etc.)
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    NotRequired,
    Protocol,
    TypedDict,
    overload,
    runtime_checkable,
)

if TYPE_CHECKING:
    from django.contrib.sessions.backends.base import SessionBase


@runtime_checkable
class DjangoModel(Protocol):
    """
    Protocol defining Django ORM methods available on all Django models.

    This Protocol provides complete type information for standard Django model
    persistence and refresh operations, both sync and async variants.

    All Django model instances (instances of models.Model or subclasses) will
    match this protocol because they implement these methods.

    Methods:
        - save: Persist model instance to database
        - delete: Delete model instance from database
        - refresh_from_db: Reload instance fields from database
        - full_clean: Validate model instance (calls validation methods)
        - asave: Async variant of save
        - adelete: Async variant of delete
        - arefresh_from_db: Async variant of refresh_from_db
    """

    # ORM methods
    def save(
        self,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: list[str] | None = None,
    ) -> None: ...
    def delete(self, using: str | None = None, keep_parents: bool = False) -> tuple[int, dict[str, int]]: ...
    def refresh_from_db(self, using: str | None = None, fields: list[str] | None = None) -> None: ...
    def full_clean(self, exclude: list[str] | None = None, validate_unique: bool = True) -> None: ...

    # Async ORM methods
    async def asave(
        self,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: list[str] | None = None,
    ) -> None: ...
    async def adelete(self, using: str | None = None, keep_parents: bool = False) -> tuple[int, dict[str, int]]: ...
    async def arefresh_from_db(self, using: str | None = None, fields: list[str] | None = None) -> None: ...


class UserType(DjangoModel, Protocol):
    """
    Protocol matching Django's AbstractBaseUser interface.

    Provides complete type information for:
    - All user fields (username, email, password, is_staff, is_superuser, etc.)
    - All user methods (check_password, set_password, etc.)
    - All Django ORM methods (inherited from DjangoModel: save, delete, asave, adelete, etc.)

    This Protocol works as a structural type - any Django user model
    will match it because it has all these fields and methods.

    UserType inherits ORM methods from DjangoModel, providing a complete
    interface for authenticated user objects with persistence capabilities.
    """

    # Core fields
    id: int
    pk: int
    is_active: bool

    # User fields
    email: str
    first_name: str
    last_name: str
    is_staff: bool
    is_superuser: bool

    # Properties
    @property
    def is_anonymous(self) -> bool: ...

    @property
    def is_authenticated(self) -> bool: ...

    # User auth methods
    def set_password(self, raw_password: str | None) -> None: ...
    def check_password(self, raw_password: str) -> bool: ...
    def set_unusable_password(self) -> None: ...
    def has_usable_password(self) -> bool: ...
    def get_session_auth_hash(self) -> str: ...


class AuthContext(Protocol):
    """
    Protocol defining the authentication context passed with authenticated requests.

    The auth context contains information extracted from authentication credentials
    (JWT tokens, API keys, session data, etc.) and is available via:
    - request.context (property)
    - request.get("auth") or request.get("context") (dict-like access)

    Mirrors the dataclass from django_bolt.auth.backends.AuthContext but as a Protocol
    for better type checking and IDE support.

    Example:
        ```python
        @api.get("/me", guards=[IsAuthenticated()])
        async def get_me(request: Request) -> dict:
            ctx: AuthContext = request.context
            return {
                "user_id": ctx.user_id,
                "is_staff": ctx.is_staff,
                "backend": ctx.backend,
                "permissions": ctx.permissions or set(),
            }
        ```
    """

    # Core fields
    user_id: str | None
    """User identifier extracted from credentials (user ID, API key ID, etc.)"""

    is_staff: bool
    """Whether the user has staff privileges"""

    is_superuser: bool
    """Whether the user is a superuser"""

    backend: str
    """Authentication backend used: 'jwt', 'api_key', 'session', etc."""

    # Optional fields
    permissions: set[str] | None
    """User permissions/scopes (optional, may be provided by some backends)"""

    claims: dict[str, Any] | None
    """Full claims dict (optional, e.g., JWT claims for JWT authentication)"""


class Request(Protocol):
    """
    Django-Bolt request object (Rust-backed PyRequest).

    Provides dict-like access to HTTP request data with full type safety.
    This is a Protocol that matches the Rust PyRequest implementation,
    enabling proper type hints and IDE autocomplete.

    Example:
        ```python
        from django_bolt import BoltAPI, Request
        import msgspec

        class UserCreate(msgspec.Struct):
            name: str
            email: str

        api = BoltAPI()

        # With request object and validated body
        @api.post("/users")
        async def create_user(request: Request, user: UserCreate):
            # Type-safe access to request data
            method = request.method          # str
            auth = request.get("auth")       # Optional[Dict[str, Any]]
            headers = request["headers"]     # Dict[str, str]

            # Validated body with full type safety
            name = user.name                 # str
            email = user.email               # str

            return {"id": 1, "name": name}

        # With just request object
        @api.get("/users/{user_id}")
        async def get_user(request: Request, user_id: int):
            auth = request.get("auth", {})
            user_id = auth.get("user_id")
            return {"user_id": user_id}
        ```

    Available Properties:
        - method: HTTP method (str)
        - path: Request path (str)
        - body: Raw request body (bytes)
        - context: Authentication context (Optional[AuthContext] - see :class:`AuthContext`)
        - user: Lazy-loaded Django user object (Optional[UserType] - see :class:`UserType`)

    Available Keys (for .get() and [] access):
        - "method": HTTP method (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS)
        - "path": Request path (/users/123)
        - "body": Raw request body (bytes)
        - "params": Path parameters from URL pattern (Dict[str, str])
        - "query": Query string parameters (Dict[str, str])
        - "headers": HTTP headers (Dict[str, str])
        - "cookies": Parsed cookies (Dict[str, str])
        - "auth": Authentication context (Optional[Dict[str, Any]])
        - "context": Same as "auth" (alias)

    Auth Context Structure (when authentication is present):
        ```python
        {
            "user_id": "123",                # User identifier (str)
            "is_staff": False,               # Staff status (bool)
            "is_superuser": False,           # Superuser status (bool)
            "auth_backend": "jwt",           # Backend used: jwt, api_key, etc.
            "permissions": ["read", "write"], # User permissions (List[str], optional)
            "auth_claims": {                 # Full JWT claims (optional, JWT only)
                "sub": "123",
                "exp": 1234567890,
                "iat": 1234567800,
                # ... additional claims
            }
        }
        ```
    """

    # Properties (from Rust #[getter])
    @property
    def method(self) -> str:
        """
        HTTP method.

        Returns:
            HTTP method string: "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"

        Example:
            ```python
            if request.method == "POST":
                # Handle POST request
                pass
            ```
        """
        ...

    @property
    def path(self) -> str:
        """
        Request path.

        Returns:
            Request path string (e.g., "/users/123")

        Example:
            ```python
            path = request.path  # "/api/users/123"
            ```
        """
        ...

    @property
    def body(self) -> bytes:
        """
        Raw request body.

        Returns:
            Request body as bytes

        Note:
            For JSON requests, the framework automatically decodes this
            when you use msgspec.Struct parameter types. You typically
            don't need to access this directly.

        Example:
            ```python
            raw_body = request.body
            # b'{"name": "John", "email": "john@example.com"}'
            ```
        """
        ...

    @property
    def context(self) -> AuthContext | None:
        """
        Authentication/middleware context with full type information.

        Returns an AuthContext Protocol object when authentication is present, None otherwise.
        The AuthContext provides typed access to authentication data:
        - user_id (Optional[str]): User identifier
        - is_staff (bool): Staff status
        - is_superuser (bool): Superuser status
        - backend (str): Backend used (jwt, api_key, session, etc.)
        - permissions (Optional[set[str]]): User permissions/scopes (if available)
        - claims (Optional[Dict[str, Any]]): Full claims dict (if JWT authentication)

        Example:
            ```python
            @api.get("/info", guards=[IsAuthenticated()])
            async def get_info(request: Request) -> dict:
                ctx: AuthContext = request.context  # Type-checked access
                return {
                    "user_id": ctx.user_id,
                    "is_staff": ctx.is_staff,
                    "backend": ctx.backend,
                    "permissions": ctx.permissions or set(),
                }
            ```
        """
        ...

    @property
    def user(self) -> UserType | None:
        """
        Lazy-loaded Django user object from authentication context.

        Returns a LazyUser proxy that acts as a transparent wrapper for a Django user model
        instance (AbstractBaseUser or custom user model). The user is loaded from the database
        only when first accessed (like Django QuerySets), avoiding wasted queries for handlers
        that don't use the user.

        The LazyUser proxy is transparent - accessing any attribute (username, email, is_staff,
        etc.) will trigger user loading and delegate to the underlying user instance.
        From a type-checking perspective, this behaves exactly like a UserType.

        ``request.user`` is sync. The query runs on the thread that reads it. In async
        code, the query runs on the ORM pool and the event loop waits for it. Use
        ``await request.auser()`` in async code. It does not block the event loop, and
        ``request.user`` then returns the same user with no second query. A backend
        with only an async ``get_user`` serves ``await request.auser()`` only.

        Returns:
            Django User model instance (wrapped in LazyUser proxy) if authentication context
            exists, None if no authentication or user not found.

        Performance:
            - Lazy-loaded: User is only queried from database when first accessed
            - Cached for multiple accesses within the same request
            - Zero database queries for handlers that don't access request.user

        Examples:
            ```python
            @api.get("/me", guards=[IsAuthenticated()])
            def get_me(request):
                user = request.user
                return {"username": user.username}
            ```

            In an async handler:
            ```python
            @api.get("/profile")
            async def get_profile(request):
                user = await request.auser()
                if user:
                    return {"username": user.username}
                return {"anonymous": True}
            ```
        """
        ...

    @property
    def session(self) -> "SessionBase":  # noqa: UP037 - quotes needed for forward ref
        """
        Django session object (requires SessionMiddleware).

        Use async methods in async handlers:

        Example:
            ```python
            @api.post("/counter")
            async def counter(request: Request):
                count = await request.session.aget("count", 0)
                await request.session.aset("count", count + 1)
                return {"count": count + 1}
            ```
        """
        ...

    # Methods
    @overload
    def get(self, key: str) -> Any:
        """Get request attribute (returns None if not found)."""
        ...

    @overload
    def get(self, key: str, default: Any) -> Any:
        """Get request attribute with default value."""
        ...

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get request attribute with optional default value.

        This method provides dict-like .get() access to request data,
        with support for default values when keys don't exist.

        Args:
            key: Attribute name to retrieve. Available keys:
                - "method": HTTP method (str)
                - "path": Request path (str)
                - "body": Raw body (bytes)
                - "params": Path parameters (Dict[str, str])
                - "query": Query parameters (Dict[str, str])
                - "headers": HTTP headers (Dict[str, str])
                - "cookies": Parsed cookies (Dict[str, str])
                - "auth": Auth context (Optional[Dict[str, Any]])
                - "context": Same as "auth"
            default: Default value to return if key doesn't exist or is None.
                     Defaults to None.

        Returns:
            Attribute value if present, otherwise default value.

        Special Behavior:
            For "auth" and "context" keys, if authentication is not configured
            or no credentials provided, returns the default value (not an empty dict).

        Example:
            ```python
            # Get with None as default
            auth = request.get("auth")  # None if no auth

            # Get with custom default
            auth = request.get("auth", {})  # {} if no auth

            # Get method (always present)
            method = request.get("method")  # "GET", "POST", etc.

            # Get query params
            query = request.get("query", {})
            page = query.get("page", "1")
            ```
        """
        ...

    def __getitem__(self, key: str) -> Any:
        """
        Dict-style access to request attributes.

        Args:
            key: Attribute name (same keys as .get())

        Returns:
            Attribute value

        Raises:
            KeyError: If key doesn't exist

        Example:
            ```python
            method = request["method"]      # "GET"
            headers = request["headers"]    # Dict[str, str]
            params = request["params"]      # Dict[str, str]

            # Raises KeyError if no auth
            context = request["context"]

            # Safe alternative with .get()
            context = request.get("context", {})
            ```
        """
        ...


# ═══════════════════════════════════════════════════════════════════════════
# Authentication Type Aliases
# ═══════════════════════════════════════════════════════════════════════════


class JWTClaims(TypedDict, total=False):
    """
    Standard JWT claims structure.

    See RFC 7519 for standard claim definitions.
    Use this as the AuthT type parameter for JWT-authenticated requests.

    Example:
        @api.get("/profile")
        async def profile(request: Request[User, JWTClaims, dict]) -> dict:
            user_id = request.auth["sub"]  # IDE knows this is str
            exp = request.auth["exp"]       # IDE knows this is int
            return {"user_id": user_id}
    """

    # Registered claims (RFC 7519)
    sub: str  # Subject (typically user ID)
    exp: int  # Expiration time (Unix timestamp)
    iat: int  # Issued at (Unix timestamp)
    nbf: int  # Not before (Unix timestamp)
    iss: str  # Issuer
    aud: str  # Audience
    jti: str  # JWT ID (unique identifier)

    # Common custom claims
    user_id: int
    username: str
    email: str
    is_staff: bool
    is_superuser: bool
    permissions: list[str]
    groups: list[str]


class APIKeyAuth(TypedDict, total=False):
    """
    API key authentication context.

    Use this as the AuthT type parameter for API key-authenticated requests.

    Example:
        @api.get("/data", auth=[APIKeyAuthentication()])
        async def get_data(request: Request[None, APIKeyAuth, dict]) -> dict:
            key_id = request.auth["key_id"]
            permissions = request.auth.get("permissions", [])
            return {"key": key_id}
    """

    key_id: str
    key_name: str
    permissions: list[str]
    rate_limit: NotRequired[int]
    metadata: NotRequired[dict[str, Any]]


# ═══════════════════════════════════════════════════════════════════════════
# Custom State Type Examples
# ═══════════════════════════════════════════════════════════════════════════


class TimingState(TypedDict, total=False):
    """
    State for timing middleware.

    Example:
        @api.get("/timed")
        async def timed(request: Request[User, Auth, TimingState]) -> dict:
            request_id = request.state.request_id  # IDE autocomplete
            start = request.state.start_time
            return {"request_id": request_id}
    """

    start_time: float
    request_id: str


class TracingState(TypedDict, total=False):
    """
    State for distributed tracing.

    Example:
        @api.get("/traced")
        async def traced(request: Request[User, Auth, TracingState]) -> dict:
            trace_id = request.state.trace_id
            return {"trace_id": trace_id}
    """

    trace_id: str
    span_id: str
    parent_span_id: NotRequired[str]
    baggage: NotRequired[dict[str, str]]


# ═══════════════════════════════════════════════════════════════════════════
# Middleware Response Types
# ═══════════════════════════════════════════════════════════════════════════


class MiddlewareResponse(TypedDict, total=False):
    """
    Type for middleware response objects.

    Used internally by middleware to return responses.

    Note: set_cookies is a list to support multiple Set-Cookie headers.
    HTTP allows multiple Set-Cookie headers, but dict can't have duplicate keys.
    """

    body: bytes
    status_code: int
    headers: dict[str, str]
    set_cookies: list[str]


__all__ = [
    # Protocols
    "Request",
    "UserType",
    "AuthContext",
    "DjangoModel",
    # JWT Types
    "JWTClaims",
    # API Key Types
    "APIKeyAuth",
    # State Types
    "TimingState",
    "TracingState",
    # Response Types
    "MiddlewareResponse",
]
