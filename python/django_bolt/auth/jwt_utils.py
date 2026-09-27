"""
JWT utility functions for Django-Bolt.

Provides helper functions to create JWT tokens for Django users and
extract user information from request context.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated, Any

import jwt
from django.conf import settings

from django_bolt.exceptions import Unauthorized
from django_bolt.params import Depends
from django_bolt.types import Request

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


def create_jwt_for_user(
    user,
    secret: str | None = None,
    algorithm: str = "HS256",
    expires_in: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """
    Create a JWT token for a Django User.

    Args:
        user: Django User model instance
        secret: JWT secret key. If None, uses Django's SECRET_KEY
        algorithm: JWT algorithm (default: "HS256")
        expires_in: Token expiration time in seconds (default: 3600 = 1 hour)
        extra_claims: Additional claims to include in the token

    Returns:
        JWT token string

    Standard claims included:
        - sub: user.id (subject - user primary key)
        - exp: expiration time (current time + expires_in)
        - iat: issued at time (current timestamp)
        - is_staff: user.is_staff
        - is_superuser: user.is_superuser
        - username: user.username (for reference)
        - email: user.email (if available)

    Example:
        ```python
        from django.contrib.auth import get_user_model
        from django_bolt.jwt_utils import create_jwt_for_user

        User = get_user_model()
        user = await User.objects.aget(username="john")

        # Create a basic token
        token = create_jwt_for_user(user)

        # Create token with custom expiration and extra claims
        token = create_jwt_for_user(
            user,
            expires_in=7200,  # 2 hours
            extra_claims={
                "permissions": ["read", "write"],
                "role": "admin",
                "tenant_id": "acme-corp"
            }
        )
        ```
    """
    # Use Django SECRET_KEY if no secret provided
    if secret is None:
        secret = settings.SECRET_KEY

    # Build standard claims
    now = int(time.time())
    payload = {
        "sub": str(user.id),  # Subject: user ID as string
        "exp": now + expires_in,  # Expiration time
        "iat": now,  # Issued at
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "username": user.username,
    }

    # Add email if available
    if hasattr(user, "email") and user.email:
        payload["email"] = user.email

    # Add first/last name if available
    if hasattr(user, "first_name") and user.first_name:
        payload["first_name"] = user.first_name
    if hasattr(user, "last_name") and user.last_name:
        payload["last_name"] = user.last_name

    # Merge extra claims
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, secret, algorithm=algorithm)


async def get_current_user(request: Request) -> Any:
    """
    Dependency that gives the authenticated user of the request.

    It loads the user with ``await request.auser()``. Thus it uses the user
    loader of the auth backend, runs the query on the request lane when the
    route has Django middleware, and fills the cache of ``request.user``. It
    works in sync and async handlers.

    Returns:
        The user, or None if the request is not authenticated or the user
        does not exist.

    Example:
        ```python
        from django_bolt import BoltAPI, CurrentUser
        from django_bolt.auth import IsAuthenticated, JWTAuthentication

        api = BoltAPI()

        @api.get("/me", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
        async def me(user: CurrentUser):
            return {"id": user.id, "username": user.username}
        ```

        For the type of your user model, make your own alias:
        ``Annotated[User, Depends(get_current_user)]``.
    """
    user = await request.auser()
    if user is None or not user.is_authenticated:
        return None
    return user


def get_current_user_sync(request: Request) -> Any:
    """Sync form of :func:`get_current_user`. A sync handler uses it in place of the async form.

    It reads ``request.user`` on the thread of the handler, so a sync handler
    keeps its sync dispatch, and a request with Django middleware stays on its lane.
    """
    user = request.user
    # A lazy request.user is never None itself. Its truth value is the truth
    # value of the loaded user, and a user ID with no row loads as None.
    if not user or not user.is_authenticated:
        return None
    return user


async def require_current_user(request: Request) -> Any:
    """Dependency that gives the authenticated user, or answers 401 when there is none."""
    user = await get_current_user(request)
    if user is None:
        raise Unauthorized(detail="Authentication required")
    return user


def require_current_user_sync(request: Request) -> Any:
    """Sync form of :func:`require_current_user`."""
    user = get_current_user_sync(request)
    if user is None:
        raise Unauthorized(detail="Authentication required")
    return user


# The injector of a sync handler uses the sync form of these dependencies.
get_current_user._bolt_sync_variant = get_current_user_sync
require_current_user._bolt_sync_variant = require_current_user_sync

if TYPE_CHECKING:
    CurrentUser = Annotated[AbstractBaseUser, Depends(require_current_user)]
    OptionalCurrentUser = Annotated[AbstractBaseUser | None, Depends(get_current_user)]
else:
    # The current user as a parameter: ``def me(user: CurrentUser)``. It answers 401 with no user.
    CurrentUser = Annotated[Any, Depends(require_current_user)]
    # The current user, or None with no user: ``def home(user: OptionalCurrentUser)``.
    OptionalCurrentUser = Annotated[Any, Depends(get_current_user)]


def extract_user_id_from_context(request: Request) -> str | None:
    """
    Extract user_id from request context.

    Args:
        request: Request dictionary with context

    Returns:
        User ID as string or None if not present

    Example:
        ```python
        @api.get("/data")
        async def get_data(request: dict):
            user_id = extract_user_id_from_context(request)
            if user_id:
                # Use user_id for filtering, logging, etc.
                data = await MyModel.objects.filter(user_id=user_id).all()
                return {"data": data}
            return {"error": "Not authenticated"}
        ```
    """
    context = request.get("context", {})
    return context.get("user_id")


def get_auth_context(request: Request) -> dict[str, Any]:
    """
    Get the full authentication context from request.

    Args:
        request: Request dictionary with context

    Returns:
        Authentication context dictionary containing:
        - user_id: User identifier
        - is_staff: Staff status boolean
        - is_superuser: Superuser status boolean
        - auth_backend: Authentication backend used (jwt, api_key, etc.)
        - permissions: List of permissions (if available)
        - auth_claims: JWT claims dict (if JWT auth was used)

    Example:
        ```python
        @api.get("/admin/stats")
        async def admin_stats(request: dict):
            auth_ctx = get_auth_context(request)

            if not auth_ctx.get("is_superuser"):
                return {"error": "Admin access required"}

            # Use user_id for audit logging
            user_id = auth_ctx["user_id"]
            backend = auth_ctx["auth_backend"]

            return {
                "authenticated_as": user_id,
                "via": backend,
                "stats": {...}
            }
        ```
    """
    return request.get("context", {})
