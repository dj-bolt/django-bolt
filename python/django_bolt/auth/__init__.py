"""
Django-Bolt Authentication and Authorization System.

High-performance auth system where validation happens in Rust without the GIL.
Python classes define configuration that gets compiled to Rust types.
"""

# Authentication backends
from .backends import (
    APIKeyAuthentication,
    AuthContext,
    BaseAuthentication,
    JWTAuthentication,
    get_default_authentication_classes,
)

# Permission guards
from .guards import (
    AllowAny,
    BasePermission,
    IsAuthenticated,
    Requires,
    get_default_permission_classes,
)

# JWT utilities for Django User integration
from .jwt_utils import (
    create_jwt_for_user,
    extract_user_id_from_context,
    get_auth_context,
    get_current_user,
)

# Token revocation (optional)
from .revocation import (
    DjangoCacheRevocation,
    DjangoORMRevocation,
    InMemoryRevocation,
    RevocationStore,
    create_revocation_handler,
)

# JWT Token handling
from .token import Token

# Access + refresh token lifecycle
from .tokens import (
    TokenPair,
    TokenRotationError,
    create_token_pair,
    rotate_refresh_token,
    set_token_cookies,
)

# User loading for request.user
from .user_loader import (
    get_registered_backend,
    load_user,
    register_auth_backend,
)
from .views import JWTAuthViews, LoginCredentials

__all__ = [
    # Authentication
    "BaseAuthentication",
    "JWTAuthentication",
    "APIKeyAuthentication",
    "AuthContext",
    "get_default_authentication_classes",
    # Guards/Permissions
    "BasePermission",
    "AllowAny",
    "IsAuthenticated",
    "Requires",
    "get_default_permission_classes",
    # JWT
    "Token",
    "create_jwt_for_user",
    "get_current_user",
    "extract_user_id_from_context",
    "get_auth_context",
    # Token pair lifecycle (access + refresh)
    "TokenPair",
    "TokenRotationError",
    "create_token_pair",
    "rotate_refresh_token",
    "set_token_cookies",
    # Revocation (optional)
    "RevocationStore",
    "InMemoryRevocation",
    "DjangoCacheRevocation",
    "DjangoORMRevocation",
    "create_revocation_handler",
    # User loading
    "register_auth_backend",
    "get_registered_backend",
    "load_user",
    # Ready-made lifecycle endpoints
    "JWTAuthViews",
    "LoginCredentials",
]
