"""
Async user fallback for requests without Django authentication middleware.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import AnonymousUser


async def auser_fallback(user: Any = None) -> Any:
    """
    Return the Bolt user, or an anonymous user if no user is set.

    Keep the same lazy user object as request.user to share its cached result.
    """
    return user if user is not None else AnonymousUser()


__all__ = ["auser_fallback"]
