"""
JWT utility functions for Django-Bolt.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model

from django_bolt.types import Request


def create_jwt_for_user(
    user,
    secret: str | None = None,
    algorithm: str = "HS256",
    expires_in: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a JWT token for a Django User."""
    if secret is None:
        secret = settings.SECRET_KEY

    now = int(time.time())
    payload = {
        "sub": str(user.id),
        "exp": now + expires_in,
        "iat": now,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "username": user.username,
    }

    if hasattr(user, "email") and user.email:
        payload["email"] = user.email
    if hasattr(user, "first_name") and user.first_name:
        payload["first_name"] = user.first_name
    if hasattr(user, "last_name") and user.last_name:
        payload["last_name"] = user.last_name
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, secret, algorithm=algorithm)


async def get_current_user(request: Request):
    """Extract and fetch Django User from request context."""
    User = get_user_model()
    context = request.get("context", {})
    user_id = context.get("user_id")

    if not user_id:
        return None

    try:
        user = await User.objects.aget(pk=user_id)
        return user
    except (User.DoesNotExist, ValueError, TypeError):
        return None


def extract_user_id_from_context(request: Request) -> str | None:
    """Extract user_id from request context."""
    context = request.get("context", {})
    return context.get("user_id")


def get_auth_context(request: Request) -> dict[str, Any]:
    """Get the full authentication context from request."""
    return request.get("context", {})


def extract_token_from_header(authorization: str | None) -> str | None:
    """
    Safely extract JWT token from Authorization header.

    Args:
        authorization: Authorization header value (e.g., "Bearer <token>")

    Returns:
        Token string or None if header is missing or malformed
    """
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def decode_jwt(
    token: str,
    secret: str | None = None,
    algorithms: list[str] | None = None,
    leeway: int = 10,
) -> dict[str, Any]:
    """
    Safely decode a JWT token with algorithm enforcement and leeway.

    Args:
        token: JWT token string
        secret: Secret key. If None, uses Django's SECRET_KEY
        algorithms: Allowed algorithms (default: ["HS256"])
        leeway: Clock skew tolerance in seconds (default: 10)

    Returns:
        Decoded payload dict, or empty dict if invalid
    """
    if secret is None:
        secret = settings.SECRET_KEY
    if algorithms is None:
        algorithms = ["HS256"]

    try:
        return jwt.decode(
            token,
            secret,
            algorithms=algorithms,
            options={"leeway": leeway},
        )
    except jwt.PyJWTError:
        return {}