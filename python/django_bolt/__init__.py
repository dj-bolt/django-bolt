"""
Django-Bolt: High-performance API framework for Django.

Provides Rust-powered API endpoints with 60k+ RPS performance, integrating
with existing Django projects via Actix Web, PyO3, and msgspec.

Quick Start:
    from django_bolt import BoltAPI, Request

    api = BoltAPI()

    @api.get("/hello")
    async def hello(request: Request) -> dict:
        return {"message": "Hello, World!"}

Type-Safe Requests:
    from django_bolt import BoltAPI, Request
    from django_bolt.types import JWTClaims
    from myapp.models import User

    api = BoltAPI()

    @api.get("/profile", guards=[IsAuthenticated()])
    async def profile(request: Request[User, JWTClaims, dict]) -> dict:
        return {"email": request.user.email}  # IDE knows User has email

Middleware:
    from django_bolt import BoltAPI
    from django_bolt.middleware import (
        DjangoMiddleware,
        TimingMiddleware,
        LoggingMiddleware,
    )
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.contrib.auth.middleware import AuthenticationMiddleware

    api = BoltAPI(
        middleware=[
            DjangoMiddleware(SessionMiddleware),
            DjangoMiddleware(AuthenticationMiddleware),
            TimingMiddleware,
            LoggingMiddleware,
        ]
    )
"""

# Status codes module
from . import status_codes as status
from .api import BoltAPI

# Auth module
from .auth import (
    # Guards/Permissions
    AllowAny,
    APIKeyAuthentication,
    AuthContext,
    HasAllPermissions,
    HasAnyPermission,
    HasPermission,
    IsAdminUser,
    IsAuthenticated,
    IsStaff,
    # Authentication backends
    JWTAuthentication,
    # JWT Token & Utilities
    Token,
    create_jwt_for_user,
    extract_user_id_from_context,
    get_auth_context,
    get_current_user,
)

# Cookies module
from .cookies import Cookie

# Datastructures
from .datastructures import UploadFile

# Decorators module
from .decorators import action

# Enums module
from .enums import FileSize, MediaType

# Middleware module
from .middleware import (
    BaseMiddleware,
    CompressionConfig,
    # Django compatibility
    DjangoMiddleware,
    ErrorHandlerMiddleware,
    LoggingMiddleware,
    Middleware,
    # Protocols and base classes
    MiddlewareProtocol,
    # Built-in middleware (Python)
    TimingMiddleware,
    cors,
    # Decorators
    middleware,
    no_compress,
    rate_limit,
    skip_middleware,
)

# OpenAPI module
from .openapi import (
    JsonRenderPlugin,
    OpenAPIConfig,
    RapidocRenderPlugin,
    RedocRenderPlugin,
    ScalarRenderPlugin,
    StoplightRenderPlugin,
    SwaggerRenderPlugin,
    YamlRenderPlugin,
)

# Pagination module
from .pagination import (
    CursorPagination,
    LimitOffsetPagination,
    PageNumberPagination,
    PaginatedResponse,
    PaginationBase,
    paginate,
)
from .params import Depends

# Type-safe Request object
from .request import Request
from .responses import JSON, Response, StreamingResponse
from .router import Router
from .types import (
    APIKeyAuth,
    DjangoModel,
    JWTClaims,
    TimingState,
    TracingState,
    UserType,
)

# Types and protocols
from .types import (
    Request as RequestProtocol,  # Protocol for type checking
)

# Views module
from .views import (
    APIView,
    CreateMixin,
    DestroyMixin,
    ListMixin,
    ModelViewSet,
    PartialUpdateMixin,
    ReadOnlyModelViewSet,
    RetrieveMixin,
    UpdateMixin,
    ViewSet,
)

# WebSocket module
from .websocket import (
    CloseCode,
    WebSocket,
    WebSocketClose,
    WebSocketDisconnect,
    WebSocketException,
    WebSocketState,
)

__all__ = [
    # Core
    "BoltAPI",
    "Request",
    "Response",
    "JSON",
    "StreamingResponse",
    "CompressionConfig",
    "Cookie",
    "Depends",
    "UploadFile",
    # Enums
    "MediaType",
    "FileSize",
    # Router
    "Router",
    # Types
    "RequestProtocol",
    "UserType",
    "AuthContext",
    "DjangoModel",
    "JWTClaims",
    "APIKeyAuth",
    "TimingState",
    "TracingState",
    # Views
    "APIView",
    "ViewSet",
    "ModelViewSet",
    "ReadOnlyModelViewSet",
    "ListMixin",
    "RetrieveMixin",
    "CreateMixin",
    "UpdateMixin",
    "PartialUpdateMixin",
    "DestroyMixin",
    # Pagination
    "PaginationBase",
    "PageNumberPagination",
    "LimitOffsetPagination",
    "CursorPagination",
    "PaginatedResponse",
    "paginate",
    # Decorators
    "action",
    # Auth - Authentication
    "JWTAuthentication",
    "APIKeyAuthentication",
    "AuthContext",
    # Auth - Guards/Permissions
    "AllowAny",
    "IsAuthenticated",
    "IsAdminUser",
    "IsStaff",
    "HasPermission",
    "HasAnyPermission",
    "HasAllPermissions",
    # Middleware - Protocols and base classes
    "MiddlewareProtocol",
    "BaseMiddleware",
    "Middleware",
    # Middleware - Decorators
    "middleware",
    "rate_limit",
    "cors",
    "skip_middleware",
    "no_compress",
    # Middleware - Built-in (Python)
    "TimingMiddleware",
    "LoggingMiddleware",
    "ErrorHandlerMiddleware",
    # Middleware - Django compatibility
    "DjangoMiddleware",
    # Auth - JWT Token & Utilities
    "Token",
    "create_jwt_for_user",
    "get_current_user",
    "extract_user_id_from_context",
    "get_auth_context",
    # OpenAPI
    "OpenAPIConfig",
    "SwaggerRenderPlugin",
    "RedocRenderPlugin",
    "ScalarRenderPlugin",
    "RapidocRenderPlugin",
    "StoplightRenderPlugin",
    "JsonRenderPlugin",
    "YamlRenderPlugin",
    # Status codes
    "status",
    # WebSocket
    "WebSocket",
    "WebSocketState",
    "WebSocketDisconnect",
    "WebSocketClose",
    "WebSocketException",
    "CloseCode",
]

default_app_config = "django_bolt.apps.DjangoBoltConfig"
