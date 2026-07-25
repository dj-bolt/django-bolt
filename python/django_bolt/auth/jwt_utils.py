"""
JWT utility functions for Django-Bolt.

Provides helper functions to create JWT tokens for Django users and
extract user information from request context.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model

from django_bolt.types import Request

from .pk_loader import load_user_by_pk_sync


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


async def get_current_user(request: Request):
    """
    Dependency function to extract and fetch Django User from request context.

    This is a reusable dependency that can be used with Depends() to inject
    the current authenticated Django User into your handlers.

    Args:
        request: Request dictionary with context

    Returns:
        Django User instance or None if not authenticated or not found

    Example:
        ```python
        from django_bolt import BoltAPI
        from django_bolt.auth import JWTAuthentication
        from django_bolt.permissions import IsAuthenticated
        from django_bolt.params import Depends
        from django_bolt.jwt_utils import get_current_user

        api = BoltAPI()

        @api.get(
            "/me",
            auth=[JWTAuthentication()],
            guards=[IsAuthenticated()]
        )
        async def get_my_profile(user=Depends(get_current_user)):
            return {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "is_staff": user.is_staff,
            }
        ```
    """
    User = get_user_model()
    context = request.get("context", {})
    user_id = context.get("user_id")

    if not user_id:
        return None

    # Pre-compiled pk query on the default executor. Deliberately NOT
    # `User.objects.aget`: asgiref's thread_sensitive executor is one shared
    # thread per process, which serializes every user load in the worker.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, load_user_by_pk_sync, User, user_id)


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
